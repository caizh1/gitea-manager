// 在 Gitea 所在容器内顺序读取制品，定期报告实际处理字节。
package main

import (
	"archive/tar"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"
)

type artifact struct {
	Size   int64  `json:"size"`
	Digest string `json:"sha256"`
}

type progress struct {
	Count int    `json:"files_done"`
	Total *int   `json:"files_total"`
	Bytes int64  `json:"bytes_done"`
	Size  *int64 `json:"bytes_total"`
	Item  string `json:"item"`
	last  time.Time
}

func (p *progress) publish(force bool) error {
	if !force && time.Since(p.last) < time.Second {
		return nil
	}
	p.last = time.Now()
	return json.NewEncoder(os.Stdout).Encode(p)
}

func (p *progress) Write(b []byte) (int, error) {
	p.Bytes += int64(len(b))
	return len(b), p.publish(false)
}

type fileEntry struct {
	name string
	info fs.FileInfo
}

func validName(name string) bool {
	return name != "." && filepath.IsLocal(name) && filepath.Clean(name) == name &&
		utf8.ValidString(name) && !strings.Contains(name, "\\")
}

func checkRoot(root string) error {
	for path := root; path != "/"; path = filepath.Dir(path) {
		info, err := os.Lstat(path)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return err
		}
		if !info.IsDir() {
			return fmt.Errorf("制品目录包含符号链接或特殊文件：%q", path)
		}
	}
	return nil
}

func scan(root string, p *progress) ([]fileEntry, int, int64, error) {
	var files []fileEntry
	var count int
	var size int64
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, err error) error {
		if path == root && os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		name, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if (name != "." && !validName(name)) || (!info.IsDir() && !info.Mode().IsRegular()) {
			return fmt.Errorf("制品目录包含不受支持的条目：%q", name)
		}
		files = append(files, fileEntry{name, info})
		if !info.IsDir() {
			count++
			size += info.Size()
		}
		return p.publish(false)
	})
	return files, count, size, err
}

func readArtifact(path string, info fs.FileInfo, destination io.Writer, p *progress, buffer []byte) (artifact, error) {
	file, err := os.Open(path)
	if err != nil {
		return artifact{}, err
	}
	defer file.Close()
	actual, err := file.Stat()
	if err != nil {
		return artifact{}, err
	}
	if !os.SameFile(info, actual) || !actual.Mode().IsRegular() || info.Size() != actual.Size() {
		return artifact{}, fmt.Errorf("打开制品时文件已变化：%q", p.Item)
	}
	digest := sha256.New()
	count, err := io.CopyBuffer(io.MultiWriter(destination, digest, p), file, buffer)
	if err != nil {
		return artifact{}, err
	}
	final, err := os.Lstat(path)
	if err != nil {
		return artifact{}, err
	}
	if count != info.Size() || final.Size() != info.Size() || !os.SameFile(info, final) || !info.ModTime().Equal(final.ModTime()) {
		return artifact{}, fmt.Errorf("读取制品期间文件已变化：%q", p.Item)
	}
	p.Count++
	return artifact{count, fmt.Sprintf("%x", digest.Sum(nil))}, nil
}

func inventory(root, manifest, archive string) error {
	p := &progress{}
	if err := p.publish(true); err != nil {
		return err
	}
	files, count, size, err := scan(root, p)
	if err != nil {
		return err
	}
	p.Total, p.Size = &count, &size
	if err := p.publish(true); err != nil {
		return err
	}
	var tarFile *os.File
	var tarWriter *tar.Writer
	if archive != "" {
		tarFile, err = os.OpenFile(archive, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			return err
		}
		defer tarFile.Close()
		tarWriter = tar.NewWriter(tarFile)
	}
	buffer := make([]byte, 1024*1024)
	result := make(map[string]artifact, count)
	for _, entry := range files {
		var output io.Writer = io.Discard
		if tarWriter != nil {
			header, err := tar.FileInfoHeader(entry.info, "")
			if err != nil {
				return err
			}
			header.Name = filepath.Join(filepath.Base(root), entry.name)
			if err = tarWriter.WriteHeader(header); err != nil {
				return err
			}
			output = tarWriter
		}
		if entry.info.IsDir() {
			continue
		}
		p.Item = entry.name
		item, err := readArtifact(filepath.Join(root, entry.name), entry.info, output, p, buffer)
		if err != nil {
			return err
		}
		result[entry.name] = item
	}
	if tarWriter != nil {
		if err := tarWriter.Close(); err != nil {
			return err
		}
		if err := tarFile.Close(); err != nil {
			return err
		}
	}
	file, err := os.OpenFile(manifest, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if err != nil {
		return err
	}
	defer file.Close()
	if err := json.NewEncoder(file).Encode(result); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return p.publish(true)
}

func verify(root, manifest string) error {
	data, err := os.Open(manifest)
	if err != nil {
		return err
	}
	defer data.Close()
	expected := map[string]string{}
	if err := json.NewDecoder(data).Decode(&expected); err != nil {
		return err
	}
	var size int64
	count := len(expected)
	p := &progress{Total: &count}
	if err := p.publish(true); err != nil {
		return err
	}
	files := make([]fileEntry, 0, count)
	for name := range expected {
		if !validName(name) {
			return fmt.Errorf("制品校验路径无效：%q", name)
		}
		path := filepath.Join(root, name)
		if err := checkRoot(filepath.Dir(path)); err != nil {
			return err
		}
		info, err := os.Lstat(path)
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("恢复后的制品不是普通文件：%q", name)
		}
		size += info.Size()
		files = append(files, fileEntry{name, info})
		if err := p.publish(false); err != nil {
			return err
		}
	}
	p.Size = &size
	if err := p.publish(true); err != nil {
		return err
	}
	buffer := make([]byte, 1024*1024)
	for _, entry := range files {
		p.Item = entry.name
		actual, err := readArtifact(filepath.Join(root, entry.name), entry.info, io.Discard, p, buffer)
		if err != nil {
			return err
		}
		if actual.Digest != expected[entry.name] {
			return fmt.Errorf("恢复后 Actions 制品与停写点不一致：%q", entry.name)
		}
	}
	return p.publish(true)
}

func run(args []string) error {
	if len(args) != 4 || !filepath.IsAbs(args[1]) || args[1] != filepath.Clean(args[1]) || args[1] == "/" {
		return fmt.Errorf("制品操作或路径无效")
	}
	check := args[1]
	if args[0] == "source-index" || args[0] == "source-range" {
		check = filepath.Dir(check)
	}
	if err := checkRoot(check); err != nil {
		return err
	}
	switch args[0] {
	case "inventory":
		return inventory(args[1], args[2], args[3])
	case "verify":
		return verify(args[1], args[2])
	case "source-index":
		return contentChunks(args[1], os.Stdout)
	case "source-range":
		return streamRange(args[1], args[2], args[3])
	case "cache-inspect":
		return inspectCache(args[1], args[2])
	case "cache-seal":
		return sealCache(args[1], args[2], args[3])
	case "cache-prepare":
		return prepareCachedArchive(args[2], args[1], args[3])
	case "extract":
		return extractArchive(args[1], os.Stdin)
	default:
		return fmt.Errorf("制品操作无效")
	}
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
