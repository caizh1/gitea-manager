package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// 在独立真实 Git 仓库中验证对象绑定，不把模拟 HTTP 通过当作代码正确。
func fixture(t *testing.T) (context.Context, string, string) {
	t.Helper()
	t.Chdir(t.TempDir())
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	command := func(args ...string) string {
		t.Helper()
		b, e := exec.CommandContext(ctx, "git", args...).CombinedOutput()
		if e != nil {
			t.Fatalf("准备隔离仓库失败：%s", b)
		}
		return strings.TrimSpace(string(b))
	}
	command("init", "-b", "main")
	command("config", "user.name", "隔离测试")
	command("config", "user.email", "approval@example.invalid")
	if e := os.WriteFile("已审代码.txt", []byte("基线\n"), 0600); e != nil {
		t.Fatal(e)
	}
	command("add", ".")
	command("commit", "-m", "基线")
	old := command("rev-parse", "HEAD")
	if e := os.WriteFile("已审代码.txt", []byte("审批内容\n"), 0600); e != nil {
		t.Fatal(e)
	}
	command("add", ".")
	command("commit", "-m", "已审批提交")
	return ctx, old, command("rev-parse", "HEAD")
}

func TestApprovedHeadAndEquivalentTree(t *testing.T) {
	ctx, old, head := fixture(t)
	if e := proof(ctx, old, head, head); e != nil {
		t.Fatal(e)
	}
	tree, e := git(ctx, "rev-parse", head+"^{tree}")
	if e != nil {
		t.Fatal(e)
	}
	squashed, e := git(ctx, "commit-tree", tree, "-p", old, "-m", "压缩合并")
	if e != nil {
		t.Fatal(e)
	}
	if e = proof(ctx, old, squashed, head); e != nil {
		t.Fatal(e)
	}
}

func TestUnapprovedCodeRejected(t *testing.T) {
	ctx, old, head := fixture(t)
	if e := os.WriteFile(filepath.Join("未审内容.txt"), []byte("必须拒绝\n"), 0600); e != nil {
		t.Fatal(e)
	}
	if _, e := git(ctx, "add", "."); e != nil {
		t.Fatal(e)
	}
	if _, e := git(ctx, "commit", "-m", "额外未审提交"); e != nil {
		t.Fatal(e)
	}
	extra, e := git(ctx, "rev-parse", "HEAD")
	if e != nil {
		t.Fatal(e)
	}
	if proof(ctx, old, extra, head) == nil {
		t.Fatal("错误接受未审内容")
	}
}

func TestNonFastForwardMalformedAndTimeout(t *testing.T) {
	ctx, old, head := fixture(t)
	if proof(ctx, head, old, head) == nil {
		t.Fatal("错误接受回退目标分支")
	}
	if proof(ctx, old, "--help", head) == nil {
		t.Fatal("错误接受命令参数注入")
	}
	expired, cancel := context.WithCancel(ctx)
	cancel()
	if proof(expired, old, head, head) == nil {
		t.Fatal("预算已过期仍放行")
	}
}

func TestBoundedOutput(t *testing.T) {
	ctx, old, _ := fixture(t)
	if _, e := git(ctx, "cat-file", "-t", old); e != nil {
		t.Fatal(e)
	}
	if !sha.MatchString(old) || sha.MatchString(old+"\n") {
		t.Fatal("对象校验边界错误")
	}
}
