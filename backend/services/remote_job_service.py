import logging
import shlex
import time
from dataclasses import dataclass, field

from config import (
    RESTORE_DIAGNOSTIC_INTERVAL_SECONDS,
    RESTORE_JOB_POLL_SECONDS,
    RESTORE_LONG_JOB_TIMEOUT_SECONDS,
    RESTORE_SSH_RECONNECT_GRACE_SECONDS,
)


REMOTE_LOG_TAIL_BYTES = 6000


@dataclass
class RemoteJobStatus:
    state: str
    exit_code: int | None = None
    pid: int | None = None
    child_pid: int | None = None
    started_epoch: int | None = None
    finished_epoch: int | None = None
    stdout_size: int = 0
    stderr_size: int = 0


@dataclass
class RemoteJobResult:
    job_dir: str
    exit_code: int
    elapsed_seconds: int
    stdout_tail: str = ''
    stderr_tail: str = ''
    metrics: dict = field(default_factory=dict)


class RemoteJobError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


class RemoteJobTimeout(RemoteJobError):
    pass


def _parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_status(raw):
    values = {}
    for line in (raw or '').splitlines():
        key, separator, value = line.partition('=')
        if separator:
            values[key.strip()] = value.strip()
    return RemoteJobStatus(
        state=values.get('state', 'unknown'),
        exit_code=_parse_int(values.get('exit_code')),
        pid=_parse_int(values.get('pid')),
        child_pid=_parse_int(values.get('child_pid')),
        started_epoch=_parse_int(values.get('started_at')),
        finished_epoch=_parse_int(values.get('finished_at')),
        stdout_size=_parse_int(values.get('stdout_size')) or 0,
        stderr_size=_parse_int(values.get('stderr_size')) or 0,
    )


def _wrapper_script(job_dir, command):
    quoted_dir = shlex.quote(job_dir)
    quoted_command = shlex.quote(command)
    return f'''#!/bin/sh
# Gitea Manager 远端恢复步骤包装器：输出和状态均保留在任务目录。
umask 077
job_dir={quoted_dir}
cd "$job_dir" || exit 125
date +%s > started_at
printf '%s\n' "$$" > pid
sh -c {quoted_command} > stdout.log 2> stderr.log &
child_pid=$!
printf '%s\n' "$child_pid" > child_pid
wait "$child_pid"
job_status=$?
date +%s > finished_at
exit_tmp="exit_code.tmp.$$"
printf '%s\n' "$job_status" > "$exit_tmp"
mv "$exit_tmp" exit_code
exit "$job_status"
'''


def start_remote_job(ssh, job_dir, command):
    quoted_dir = shlex.quote(job_dir)
    exit_code, out, err = ssh.exec(f'mkdir -p {quoted_dir}', timeout=60)
    if exit_code != 0:
        raise RemoteJobError(f'创建远端作业目录失败: {err or out or exit_code}')

    script_path = f'{job_dir}/run.sh'
    ssh.put_text(script_path, _wrapper_script(job_dir, command), mode=0o700)
    quoted_script = shlex.quote(script_path)
    launch = (
        f'if mkdir {quoted_dir}/launch.lock 2>/dev/null; then '
        f'nohup setsid /bin/sh {quoted_script} >/dev/null 2>&1 < /dev/null & '
        f'printf "started=%s\\n" "$!"; '
        f'else printf "started=existing\\n"; fi'
    )
    exit_code, out, err = ssh.exec(launch, timeout=60)
    if exit_code != 0:
        raise RemoteJobError(f'启动远端作业失败: {err or out or exit_code}')
    return out


def get_remote_job_status(ssh, job_dir):
    quoted_dir = shlex.quote(job_dir)
    command = f'''
job_dir={quoted_dir}
state=starting
if test -s "$job_dir/exit_code"; then
  state=done
elif test -s "$job_dir/child_pid" && kill -0 "$(cat "$job_dir/child_pid")" 2>/dev/null; then
  state=running
elif test -s "$job_dir/pid" && kill -0 "$(cat "$job_dir/pid")" 2>/dev/null; then
  state=running
elif test -d "$job_dir/launch.lock"; then
  state=lost
else
  state=not_started
fi
printf 'state=%s\n' "$state"
for item in exit_code pid child_pid started_at finished_at; do
  if test -s "$job_dir/$item"; then printf '%s=%s\n' "$item" "$(cat "$job_dir/$item")"; fi
done
for item in stdout stderr; do
  size=0
  if test -f "$job_dir/$item.log"; then size=$(wc -c < "$job_dir/$item.log"); fi
  printf '%s_size=%s\n' "$item" "$size"
done
'''
    exit_code, out, err = ssh.exec(command, timeout=60)
    if exit_code != 0:
        raise RemoteJobError(f'读取远端作业状态失败: {err or out or exit_code}')
    return _parse_status(out)


def read_remote_job_tails(ssh, job_dir, max_bytes=REMOTE_LOG_TAIL_BYTES):
    quoted_dir = shlex.quote(job_dir)
    max_bytes = max(256, min(int(max_bytes), 20000))
    command = (
        f'printf "__STDOUT__\\n"; tail -c {max_bytes} {quoted_dir}/stdout.log 2>/dev/null || true; '
        f'printf "\\n__STDERR__\\n"; tail -c {max_bytes} {quoted_dir}/stderr.log 2>/dev/null || true'
    )
    _, out, _ = ssh.exec(command, timeout=60, output_limit=max_bytes * 2 + 1024)
    stdout_part, marker, stderr_part = out.partition('__STDERR__')
    stdout_part = stdout_part.replace('__STDOUT__', '', 1).strip()
    return stdout_part, stderr_part.strip() if marker else ''


def terminate_remote_job(ssh, job_dir):
    quoted_dir = shlex.quote(job_dir)
    command = f'''
job_dir={quoted_dir}
for file in child_pid pid; do
  if test -s "$job_dir/$file"; then
    target_pid=$(cat "$job_dir/$file")
    kill -TERM "$target_pid" 2>/dev/null || true
  fi
done
if test -s "$job_dir/pid"; then
  job_pid=$(cat "$job_dir/pid")
  kill -TERM -- "-$job_pid" 2>/dev/null || true
fi
sleep 2
for file in child_pid pid; do
  if test -s "$job_dir/$file"; then
    target_pid=$(cat "$job_dir/$file")
    kill -KILL "$target_pid" 2>/dev/null || true
  fi
done
if test -s "$job_dir/pid"; then
  job_pid=$(cat "$job_dir/pid")
  kill -KILL -- "-$job_pid" 2>/dev/null || true
fi
'''
    ssh.exec(command, timeout=30)


def run_remote_job(
    ssh,
    job_dir,
    command,
    timeout_seconds=RESTORE_LONG_JOB_TIMEOUT_SECONDS,
    poll_seconds=RESTORE_JOB_POLL_SECONDS,
    reconnect_grace_seconds=RESTORE_SSH_RECONNECT_GRACE_SECONDS,
    diagnostic_interval_seconds=RESTORE_DIAGNOSTIC_INTERVAL_SECONDS,
    progress_callback=None,
    diagnostic_callback=None,
    timeout_callback=None,
):
    start_remote_job(ssh, job_dir, command)
    started = time.monotonic()
    last_diagnostic = 0.0
    reconnect_started = None
    last_metrics = {}
    lost_started = None

    while True:
        elapsed = max(0, int(time.monotonic() - started))
        if timeout_seconds and elapsed >= timeout_seconds:
            if timeout_callback:
                try:
                    timeout_callback()
                except Exception:
                    logging.warning('[远端作业] 超时清理回调失败 - %s', job_dir, exc_info=True)
            try:
                terminate_remote_job(ssh, job_dir)
            except Exception:
                logging.warning('[远端作业] 终止失败 - %s', job_dir, exc_info=True)
            stdout_tail, stderr_tail = '', ''
            try:
                stdout_tail, stderr_tail = read_remote_job_tails(ssh, job_dir)
            except Exception:
                pass
            result = RemoteJobResult(
                job_dir=job_dir,
                exit_code=-1,
                elapsed_seconds=elapsed,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                metrics=last_metrics,
            )
            raise RemoteJobTimeout(f'远端步骤超过 {timeout_seconds} 秒，已终止: {job_dir}', result)

        try:
            status = get_remote_job_status(ssh, job_dir)
            reconnect_started = None
        except Exception as exc:
            now = time.monotonic()
            reconnect_started = reconnect_started or now
            if now - reconnect_started >= reconnect_grace_seconds:
                raise RemoteJobError(
                    f'SSH 连续 {reconnect_grace_seconds} 秒不可用，远端作业状态未知: {exc}'
                ) from exc
            if progress_callback:
                progress_callback(None, elapsed, {'SSH状态': '正在重连', '已运行秒数': elapsed})
            time.sleep(max(1, poll_seconds))
            continue

        now = time.monotonic()
        if diagnostic_callback and now - last_diagnostic >= diagnostic_interval_seconds:
            try:
                last_metrics = diagnostic_callback(status, elapsed) or {}
            except Exception as exc:
                last_metrics = {'诊断状态': f'暂不可用: {type(exc).__name__}'}
                logging.warning('[远端作业] 诊断采集失败 - %s', job_dir, exc_info=True)
            last_diagnostic = now

        base_metrics = {
            '已运行秒数': elapsed,
            '远端状态': status.state,
            'stdout字节': status.stdout_size,
            'stderr字节': status.stderr_size,
        }
        base_metrics.update(last_metrics)
        if progress_callback:
            progress_callback(status, elapsed, base_metrics)

        if status.state == 'done':
            stdout_tail, stderr_tail = read_remote_job_tails(ssh, job_dir)
            result = RemoteJobResult(
                job_dir=job_dir,
                exit_code=status.exit_code if status.exit_code is not None else -1,
                elapsed_seconds=elapsed,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                metrics=base_metrics,
            )
            if result.exit_code != 0:
                summary = stderr_tail or stdout_tail or '远端命令没有返回错误文本'
                raise RemoteJobError(
                    f'远端步骤失败 (exit={result.exit_code}): {summary[-1000:]}',
                    result,
                )
            return result

        if status.state in {'lost', 'not_started', 'unknown'}:
            lost_started = lost_started or now
            if now - lost_started >= max(15, poll_seconds * 3):
                stdout_tail, stderr_tail = '', ''
                try:
                    stdout_tail, stderr_tail = read_remote_job_tails(ssh, job_dir)
                except Exception:
                    pass
                result = RemoteJobResult(
                    job_dir=job_dir,
                    exit_code=-1,
                    elapsed_seconds=elapsed,
                    stdout_tail=stdout_tail,
                    stderr_tail=stderr_tail,
                    metrics=base_metrics,
                )
                raise RemoteJobError(f'远端作业进程消失且没有退出状态: {job_dir}', result)
        else:
            lost_started = None

        time.sleep(max(1, poll_seconds))
