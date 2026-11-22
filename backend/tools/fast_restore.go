// 在备机本地合并已接收的分块并校验恢复文件。
package main

import (
	"archive/zip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

var lowerSHA = regexp.MustCompile("^[0-9a-f]{64}$")
var partName = regexp.MustCompile("^[0-9]{8}-[0-9a-f]{64}$")

type cachedChunk struct {
	Name string `json:"name"`
	Size int64  `json:"size"`
}

type cacheIndex struct {
	Digest string        `json:"sha256"`
	Size   int64         `json:"size"`
	Parts  []cachedChunk `json:"parts"`
}

func loadCacheIndex(index string) (cacheIndex, error) {
	var plan cacheIndex
	f, err := os.Open(index)
	if err != nil {
		return plan, err
	}
	defer f.Close()
	if err := json.NewDecoder(f).Decode(&plan); err != nil {
		return plan, err
	}
	if !lowerSHA.MatchString(plan.Digest) || plan.Size <= 0 || len(plan.Parts) == 0 {
		return plan, errors.New("备机归档清单无效")
	}
	var size int64
	for number, part := range plan.Parts {
		if !partName.MatchString(part.Name) || !strings.HasPrefix(part.Name, fmt.Sprintf("%08d-", number)) ||
			part.Size <= 0 || part.Size > 128<<20 || part.Size > plan.Size-size {
			return plan, errors.New("备机分块清单无效")
		}
		size += part.Size
	}
	if size != plan.Size {
		return plan, errors.New("备机分块总大小不符")
	}
	return plan, nil
}

func inspectCache(parts, index string) error {
	plan, err := loadCacheIndex(index)
	if err != nil {
		return err
	}
	encoder := json.NewEncoder(os.Stdout)
	for _, part := range plan.Parts {
		pathname := filepath.Join(parts, part.Name)
		info, err := os.Lstat(pathname)
		if err != nil || !info.Mode().IsRegular() || info.Size() != part.Size {
			continue
		}
		body, err := os.Open(pathname)
		if err != nil {
			return err
		}
		digest := sha256.New()
		_, err = io.Copy(digest, body)
		body.Close()
		if err != nil {
			return err
		}
		if hex.EncodeToString(digest.Sum(nil)) == part.Name[9:] {
			if err := encoder.Encode(map[string]string{"part": part.Name}); err != nil {
				return err
			}
		}
	}
	return encoder.Encode(map[string]bool{"complete": true})
}

func sealCache(parts, archive, index string) error {
	plan, err := loadCacheIndex(index)
	if err != nil {
		return err
	}
	work, err := os.CreateTemp(filepath.Dir(archive), ".assembling-")
	if err != nil {
		return err
	}
	defer os.Remove(work.Name())
	defer work.Close()
	digest := sha256.New()
	var size int64
	progress := &progress{Size: &plan.Size}
	for _, part := range plan.Parts {
		body, err := os.Open(filepath.Join(parts, part.Name))
		if err != nil {
			return err
		}
		hash := sha256.New()
		count, copyErr := io.Copy(io.MultiWriter(work, digest, hash, progress), io.LimitReader(body, part.Size+1))
		body.Close()
		if copyErr != nil {
			return copyErr
		}
		if count != part.Size || hex.EncodeToString(hash.Sum(nil)) != part.Name[9:] {
			return fmt.Errorf("备机分块损坏 %s", part.Name)
		}
		size += count
	}
	if size != plan.Size || hex.EncodeToString(digest.Sum(nil)) != plan.Digest {
		return errors.New("备机整包大小或摘要不符")
	}
	if err := work.Sync(); err != nil {
		return err
	}
	if err := work.Close(); err != nil {
		return err
	}
	if err := os.Rename(work.Name(), archive); err != nil {
		return err
	}
	directory, err := os.Open(filepath.Dir(archive))
	if err != nil {
		return err
	}
	defer directory.Close()
	if err := directory.Sync(); err != nil {
		return err
	}
	return progress.publish(true)
}

type restoreMember struct {
	Digest string `json:"摘要"`
	Mode   uint32 `json:"权限"`
	System uint8  `json:"系统"`
}

type restoreIndex struct {
	Version int                      `json:"版本"`
	Files   map[string]restoreMember `json:"文件"`
}

func zipBody(entry *zip.File, out io.Writer, expected string) error {
	in, err := entry.Open()
	if err != nil {
		return err
	}
	defer in.Close()
	digest := sha256.New()
	count, err := io.Copy(io.MultiWriter(out, digest), in)
	if err != nil {
		return err
	}
	if uint64(count) != entry.UncompressedSize64 || hex.EncodeToString(digest.Sum(nil)) != expected {
		return fmt.Errorf("备份 ZIP 文件内容不符 %q", entry.Name)
	}
	return nil
}

func prepareCachedArchive(filename, stage, digest string) error {
	file, err := os.Open(filename)
	if err != nil {
		return err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return err
	}
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return err
	}
	if hex.EncodeToString(hash.Sum(nil)) != digest {
		return errors.New("备机缓存整包摘要不符")
	}
	source, err := zip.NewReader(file, info.Size())
	if err != nil {
		return err
	}
	var manifest restoreIndex
	var sqlName string
	var count int
	for _, entry := range source.File {
		switch {
		case entry.Name == "manager-manifest.json":
			count++
			body, err := entry.Open()
			if err != nil {
				return err
			}
			err = json.NewDecoder(body).Decode(&manifest)
			body.Close()
			if err != nil {
				return err
			}
		case path.Base(entry.Name) == "gitea-db.sql" && !entry.FileInfo().IsDir():
			if sqlName != "" {
				return errors.New("备份必须包含唯一数据库 SQL")
			}
			sqlName = entry.Name
		}
	}
	if count != 1 || sqlName == "" || manifest.Version != 2 || len(manifest.Files) != len(source.File)-1 {
		return errors.New("备机快速恢复需要完整的备份清单")
	}
	root := strings.TrimSuffix(sqlName, "gitea-db.sql")
	const actionsRoot = "manager-actions-artifacts"
	var directories []directoryMode
	seen := make(map[string]bool, len(source.File))
	var fileTotal int
	var byteTotal int64
	done := &progress{Total: &fileTotal, Size: &byteTotal}
	for _, entry := range source.File {
		fileTotal++
		byteTotal += int64(entry.UncompressedSize64)
	}
	for _, entry := range source.File {
		name := strings.TrimSuffix(entry.Name, "/")
		mode := entry.Mode()
		if !validName(name) || seen[name] || !(mode.IsRegular() || mode.IsDir()) {
			return fmt.Errorf("备份 ZIP 包含无效路径或特殊文件 %q", name)
		}
		seen[name] = true
		if entry.Name == "manager-manifest.json" {
			done.Count++
			done.Bytes += int64(entry.UncompressedSize64)
			continue
		}
		expected, ok := manifest.Files[entry.Name]
		if !ok || uint32(entry.ExternalAttrs>>16) != expected.Mode || uint8(entry.CreatorVersion>>8) != expected.System {
			return fmt.Errorf("备份 ZIP 文件权限或清单不符 %q", name)
		}
		var destination string
		switch {
		case name == sqlName:
			destination = "incoming.sql"
		case name == root+"data" || strings.HasPrefix(name, root+"data/"):
			destination = "new-gitea" + strings.TrimPrefix(name, root+"data")
		case name == root+"repos" || strings.HasPrefix(name, root+"repos/"):
			destination = "new-repositories" + strings.TrimPrefix(name, root+"repos")
		case name == actionsRoot || strings.HasPrefix(name, actionsRoot+"/"):
			destination = "new-actions" + strings.TrimPrefix(name, actionsRoot)
		}
		done.Item = name
		writer := io.Writer(done)
		if destination == "" {
			err = zipBody(entry, writer, expected.Digest)
		} else {
			target := filepath.Join(stage, destination)
			if err := checkRoot(filepath.Dir(target)); err != nil {
				return err
			}
			if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
				return err
			}
			perm := os.FileMode(0644)
			if mode.IsDir() {
				perm = 0755
			}
			if expected.System == 3 && expected.Mode&0777 != 0 {
				perm = os.FileMode(expected.Mode & 0777)
			}
			if mode.IsDir() {
				err = os.MkdirAll(target, 0755)
				if err == nil {
					err = zipBody(entry, writer, expected.Digest)
					directories = append(directories, directoryMode{target, perm})
				}
			} else {
				var out *os.File
				out, err = os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, perm)
				if err == nil {
					err = zipBody(entry, io.MultiWriter(out, writer), expected.Digest)
					closeErr := out.Close()
					if err == nil {
						err = closeErr
					}
					if err == nil {
						err = os.Chmod(target, perm)
					}
				}
			}
		}
		if err != nil {
			return err
		}
		done.Count++
	}
	// 将目录按真正的深度排序，ZIP 可以先列子目录。
	sort.Slice(directories, func(i, j int) bool { return len(directories[i].path) > len(directories[j].path) })
	for _, item := range directories {
		if err := os.Chmod(item.path, item.mode); err != nil {
			return err
		}
	}

	for _, required := range []string{"new-gitea", "new-repositories"} {
		if item, err := os.Stat(filepath.Join(stage, required)); err != nil || !item.IsDir() {
			return fmt.Errorf("备机恢复缺少 %s", required)
		}
	}
	done.Item = ""
	return done.publish(true)
}
