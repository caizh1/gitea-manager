// 审批 Hook：只在独立验收实例强制执行，影子包装器不会调用本程序。
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Identity struct {
	ID     int `json:"id"`
	Number int `json:"number"`
}
type Config struct {
	PolicyID    int        `json:"policy_id"`
	RepoID      int        `json:"repo_id"`
	Revision    int        `json:"revision"`
	Token       string     `json:"token"`
	GateURL     string     `json:"gate_url"`
	Mode        string     `json:"mode"`
	Isolation   string     `json:"isolation"`
	Branches    []string   `json:"branches"`
	Grandfather []Identity `json:"grandfather"`
}
type Decision struct {
	Allow    bool   `json:"allow"`
	Nonce    string `json:"nonce"`
	Revision int    `json:"revision"`
	Head     string `json:"head"`
	Base     string `json:"base"`
	New      string `json:"new"`
	Error    string `json:"error"`
}

var sha = regexp.MustCompile(`^[0-9a-f]{40}$`)

func git(ctx context.Context, args ...string) (string, error) {
	cmd := exec.CommandContext(ctx, "git", args...)
	var out bytes.Buffer
	cmd.Stdout = &limitedBuffer{&out, 1024 * 1024}
	cmd.Stderr = io.Discard
	err := cmd.Run()
	if err != nil {
		return "", errors.New("实际 Git 对象校验失败")
	}
	return strings.TrimSpace(out.String()), nil
}

type limitedBuffer struct {
	b         *bytes.Buffer
	remaining int
}

func (l *limitedBuffer) Write(p []byte) (int, error) {
	if len(p) > l.remaining {
		return 0, errors.New("Git 输出超过预算")
	}
	l.remaining -= len(p)
	return l.b.Write(p)
}
func proof(ctx context.Context, old, new, head string) error {
	if !sha.MatchString(old) || !sha.MatchString(new) || !sha.MatchString(head) {
		return errors.New("对象格式错误")
	}
	// 禁止把任意旧目标或强推伪装成 PR 合并。
	if _, err := git(ctx, "merge-base", "--is-ancestor", old, new); err != nil {
		return errors.New("合并不是目标分支的快进更新")
	}
	if new == head {
		return nil
	}
	// rebase/squash 的新 SHA 与原审批 SHA 不同，用相同基线和已审 head 重新构建合入树。
	expected, err := git(ctx, "merge-tree", "--write-tree", old, head)
	if err != nil {
		return errors.New("不能确定已审代码的合入结果，未放行")
	}
	tree := strings.Split(expected, "\n")[0]
	actual, err := git(ctx, "rev-parse", new+"^{tree}")
	if err != nil || !sha.MatchString(tree) || tree != actual {
		return errors.New("实际待写入代码与已审批版本不一致")
	}
	return nil
}
func run() error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if len(os.Args) < 3 {
		return errors.New("Hook 调用参数错误")
	}
	path := os.Args[1]
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 {
		return errors.New("审批配置缺失或权限异常")
	}
	raw, err := os.ReadFile(path)
	if err != nil || len(raw) > 1024*1024 {
		return errors.New("审批配置读取失败")
	}
	var c Config
	if json.Unmarshal(raw, &c) != nil || c.Mode != "isolated_enforced" || c.Isolation == "" || len(c.Token) < 32 {
		return errors.New("未取得隔离强制审批配置")
	}
	repoID, err := strconv.Atoi(os.Getenv("GITEA_REPO_ID"))
	if err != nil || repoID != c.RepoID {
		return errors.New("仓库身份不匹配")
	}
	number, _ := strconv.Atoi(os.Getenv("GITEA_PR_INDEX"))
	prID, _ := strconv.Atoi(os.Getenv("GITEA_PR_ID"))
	var refs [][]string
	switch os.Args[2] {
	case "pre-receive":
		input, err := io.ReadAll(io.LimitReader(os.Stdin, 65537))
		if err != nil || len(input) > 65536 {
			return errors.New("引用请求超过预算")
		}
		for _, line := range strings.Split(strings.TrimSpace(string(input)), "\n") {
			f := strings.Fields(line)
			if len(f) != 3 {
				return errors.New("引用格式错误")
			}
			refs = append(refs, []string{f[2], f[0], f[1]})
		}
	case "update":
		if len(os.Args) != 6 {
			return errors.New("更新参数错误")
		}
		refs = append(refs, os.Args[3:6])
	default:
		return errors.New("不支持的 Hook 阶段")
	}
	for _, ref := range refs {
		managed := false
		for _, branch := range c.Branches {
			if ref[0] == "refs/heads/"+branch {
				managed = true
			}
		}
		if !managed {
			continue
		}
		if len(refs) > 1 {
			return errors.New("受管合并不接受多引用更新")
		}
		if number <= 0 || prID <= 0 || os.Getenv("GITEA_PUSH_TRIGGER") != "pr-merge-to-base" {
			return errors.New("受管分支只允许经过审批的 PR 合并")
		}
		legacy := false
		for _, id := range c.Grandfather {
			if id.ID == prID && id.Number == number {
				legacy = true
			}
		}
		if legacy {
			continue
		}
		if !sha.MatchString(ref[1]) || !sha.MatchString(ref[2]) {
			return errors.New("引用对象错误")
		}
		if os.Args[2] == "pre-receive" {
			continue
		} // 本地预检，无外部等待；最终 update 再读实时审批。
		u, err := url.Parse(c.GateURL)
		if err != nil || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Host == "" || (u.Scheme != "https" && !(u.Scheme == "http" && c.Isolation != "")) {
			return errors.New("门禁地址无效")
		}
		random := make([]byte, 16)
		if _, err = rand.Read(random); err != nil {
			return errors.New("请求随机数生成失败")
		}
		nonce := hex.EncodeToString(random)
		payload := map[string]any{"repo_id": c.RepoID, "revision": c.Revision, "number": number, "pr_id": prID, "old": ref[1], "new": ref[2], "ref": ref[0], "nonce": nonce, "timestamp": time.Now().Unix()}
		body, _ := json.Marshal(payload)
		req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(c.GateURL, "/")+fmt.Sprintf("/%d/check", c.PolicyID), bytes.NewReader(body))
		if err != nil {
			return err
		}
		req.Header.Set("Authorization", "Bearer "+c.Token)
		req.Header.Set("Content-Type", "application/json")
		client := &http.Client{Timeout: 8 * time.Second, Transport: &http.Transport{Proxy: nil}, CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}
		response, err := client.Do(req)
		if err != nil {
			return errors.New("审批服务不可用，暂停合并")
		}
		value, readErr := io.ReadAll(io.LimitReader(response.Body, 32769))
		response.Body.Close()
		client.CloseIdleConnections()
		if readErr != nil || len(value) > 32768 || response.StatusCode != 200 {
			return errors.New("审批核验未完成，暂停合并")
		}
		var decision Decision
		if json.Unmarshal(value, &decision) != nil || !decision.Allow || decision.Nonce != nonce || decision.Revision != c.Revision || decision.Base != ref[1] || decision.New != ref[2] {
			return errors.New("新增审批要求尚未满足或校验响应不一致")
		}
		if err = proof(ctx, ref[1], ref[2], decision.Head); err != nil {
			return err
		}
	}
	// 不将安装目录解释为仓库目录；由 Git 的实际工作目录与隔离对象环境完成校验。
	return nil
}
func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "审批门禁："+err.Error())
		os.Exit(1)
	}
}
