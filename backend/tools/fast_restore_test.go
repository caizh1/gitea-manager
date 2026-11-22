package main

import (
	"archive/zip"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"math/rand"
	"os"
	"path/filepath"
	"testing"
)

func sum(data []byte) string {
	result := sha256.Sum256(data)
	return hex.EncodeToString(result[:])
}

func saveFile(t *testing.T, filename string, body []byte) {
	t.Helper()
	if err := os.WriteFile(filename, body, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestRestoreCacheRejectsCorruptChunkAndRepairs(t *testing.T) {
	root := t.TempDir()
	first, second := []byte("甲文件"), []byte("乙文件")
	parts := []cachedChunk{
		{Name: "00000000-" + sum(first), Size: int64(len(first))},
		{Name: "00000001-" + sum(second), Size: int64(len(second))},
	}
	contents := append(append([]byte{}, first...), second...)
	index, err := json.Marshal(cacheIndex{Digest: sum(contents), Size: int64(len(contents)), Parts: parts})
	if err != nil {
		t.Fatal(err)
	}
	plan, target := filepath.Join(root, "index"), filepath.Join(root, "archive")
	saveFile(t, plan, index)
	saveFile(t, filepath.Join(root, parts[0].Name), first)
	saveFile(t, filepath.Join(root, parts[1].Name), []byte("乙文仲"))
	if err := sealCache(root, target, plan); err == nil {
		t.Fatal("损坏块不应通过整包校验")
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Fatal("校验失败不能发布成品")
	}
	saveFile(t, filepath.Join(root, parts[1].Name), second)
	if err := sealCache(root, target, plan); err != nil {
		t.Fatal(err)
	}
	actual, err := os.ReadFile(target)
	if err != nil || !bytes.Equal(actual, contents) {
		t.Fatal("补齐正确分块后未生成完整成品")
	}
}

func archiveFixture(t *testing.T, file string) string {
	t.Helper()
	var payload bytes.Buffer
	archive := zip.NewWriter(&payload)
	index := restoreIndex{Version: 2, Files: map[string]restoreMember{}}
	for name, content := range map[string]string{
		"snapshot/data/conf/app.ini":                  "配置",
		"snapshot/repos/org/project.git/HEAD":         "ref: refs/heads/main\n",
		"snapshot/gitea-db.sql":                       "SELECT 1;",
		"manager-actions-artifacts/project/asset.zip": "制品",
	} {
		header := &zip.FileHeader{Name: name, Method: zip.Deflate}
		header.SetMode(0640)
		writer, err := archive.CreateHeader(header)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := io.WriteString(writer, content); err != nil {
			t.Fatal(err)
		}
		index.Files[name] = restoreMember{
			Digest: sum([]byte(content)), Mode: header.ExternalAttrs >> 16, System: uint8(header.CreatorVersion >> 8),
		}
	}
	member, err := archive.Create("manager-manifest.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.NewEncoder(member).Encode(index); err != nil {
		t.Fatal(err)
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	saveFile(t, file, payload.Bytes())
	return sum(payload.Bytes())
}

func TestPrepareCacheLocally(t *testing.T) {
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(root, "backup.zip")
	digest := archiveFixture(t, file)
	if err := prepareCachedArchive(file, root, digest); err != nil {
		t.Fatal(err)
	}
	for name, expected := range map[string]string{
		"new-gitea/conf/app.ini":                "配置",
		"new-repositories/org/project.git/HEAD": "ref: refs/heads/main\n",
		"incoming.sql":                          "SELECT 1;",
		"new-actions/project/asset.zip":         "制品",
	} {
		got, err := os.ReadFile(filepath.Join(root, name))
		if err != nil || string(got) != expected {
			t.Fatalf("备机解包结果不符 %s %v", name, err)
		}
	}
	if err := prepareCachedArchive(file, t.TempDir(), sum(nil)); err == nil {
		t.Fatal("缓存 ZIP 摘要无效时应拒绝解包")
	}
}

func TestContentReuseAfterShift(t *testing.T) {
	body := make([]byte, 96<<20)
	rng := rand.New(rand.NewSource(19))
	rng.Read(body)
	byDigest := func(data []byte) map[string]int64 {
		in := filepath.Join(t.TempDir(), "source")
		saveFile(t, in, data)
		var out bytes.Buffer
		if err := contentChunks(in, &out); err != nil {
			t.Fatal(err)
		}
		read := json.NewDecoder(&out)
		items := make(map[string]int64)
		var total int64
		for read.More() {
			var item struct {
				Offset   int64
				Size     int64
				Complete int64
				SHA256   string
			}
			if err := read.Decode(&item); err != nil {
				t.Fatal(err)
			}
			if item.SHA256 == "" {
				if item.Complete != int64(len(data)) || item.Complete != total {
					t.Fatal("内容分块未覆盖完整归档")
				}
				break
			}
			if item.Offset != total || item.SHA256 != sum(data[item.Offset:item.Offset+item.Size]) {
				t.Fatal("内容分块偏移或摘要不符")
			}
			total += item.Size
			items[item.SHA256] = item.Size
		}
		return items
	}
	original := byDigest(body)
	shifted := byDigest(append([]byte("新增的前缀内容"), body...))
	var reusable int64
	for hash := range shifted {
		reusable += original[hash]
	}
	if reusable < 48<<20 {
		t.Fatalf("原有数据移位后的复用量过低 %d", reusable)
	}
}
