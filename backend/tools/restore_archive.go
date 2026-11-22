// 在目标暂存目录内流式解开 Manager 生成的 TAR，支持 PAX 大文件。
package main

import (
	"archive/tar"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

type directoryMode struct {
	path string
	mode os.FileMode
}

func extractArchive(root string, input io.Reader) error {
	reader := tar.NewReader(input)
	seen := map[string]bool{}
	var directories []directoryMode
	for {
		entry, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		name := strings.TrimSuffix(entry.Name, "/")
		if !validName(name) || seen[name] {
			return fmt.Errorf("恢复暂存归档包含无效或重复路径：%q", entry.Name)
		}
		seen[name] = true
		path := filepath.Join(root, name)
		if err := checkRoot(filepath.Dir(path)); err != nil {
			return err
		}
		if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
			return err
		}
		mode := entry.FileInfo().Mode().Perm()
		switch entry.Typeflag {
		case tar.TypeDir:
			if err := os.Mkdir(path, 0755); err != nil {
				if !os.IsExist(err) {
					return err
				}
				info, statErr := os.Lstat(path)
				if statErr != nil || !info.IsDir() {
					return fmt.Errorf("恢复暂存目录与既有路径冲突：%q", name)
				}
			}
			directories = append(directories, directoryMode{path, mode})
		case tar.TypeReg:
			if err := extractFile(path, mode, reader); err != nil {
				return err
			}
		default:
			return fmt.Errorf("恢复暂存归档包含非普通条目：%q", entry.Name)
		}
	}
	// 延后收紧目录权限，避免只读目录阻断子文件写入。
	for index := len(directories) - 1; index >= 0; index-- {
		item := directories[index]
		if err := os.Chmod(item.path, item.mode); err != nil {
			return err
		}
	}
	return nil
}

func extractFile(path string, mode os.FileMode, input io.Reader) error {
	output, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(output, input)
	closeErr := output.Close()
	if copyErr != nil {
		return copyErr
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Chmod(path, mode)
}
