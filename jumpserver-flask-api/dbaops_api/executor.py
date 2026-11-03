import os
import signal
import subprocess
import threading

from .runner import CommandTimeout, Completed


class LocalExecutor:
    """Run a fixed argv locally while feeding ccps through stdin.

    Output readers continuously drain both pipes but retain only a bounded
    amount. API and ccps secrets are removed from the child environment.
    """

    def __init__(self, config):
        self.config = config

    def exec(self, argv, timeout, ccps_secret):
        env = dict(os.environ)
        env.pop(self.config.api_token_env, None)
        env.pop(self.config.ccps_secret_env, None)
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        limit = self.config.max_output_bytes + 65536
        stdout = _BoundedReader(process.stdout, limit)
        stderr = _BoundedReader(process.stderr, limit)
        stdout.start()
        stderr.start()
        timed_out = False
        try:
            process.stdin.write((ccps_secret + "\n").encode("utf-8"))
            process.stdin.close()
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop_process_group(process)
            timed_out = True
        finally:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        _finish_reader(stdout)
        _finish_reader(stderr)
        if timed_out:
            raise CommandTimeout("Local operation exceeded its deadline")
        return Completed(
            process.returncode,
            bytes(stdout.data),
            bytes(stderr.data),
            stdout.truncated,
            stderr.truncated,
        )


class _BoundedReader(threading.Thread):
    def __init__(self, stream, limit):
        threading.Thread.__init__(self)
        self.daemon = True
        self.stream = stream
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def run(self):
        try:
            while True:
                chunk = self.stream.read(65536)
                if not chunk:
                    return
                remaining = self.limit - len(self.data)
                if remaining > 0:
                    self.data += chunk[:remaining]
                if len(chunk) > max(remaining, 0):
                    self.truncated = True
        finally:
            self.stream.close()


def _stop_process_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    # A descendant may retain pipes after the leader exits. Kill the entire
    # isolated group before reader cleanup, even if wait() already returned.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _finish_reader(reader):
    reader.join(timeout=2)
