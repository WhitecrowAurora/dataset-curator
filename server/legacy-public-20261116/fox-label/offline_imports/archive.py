"""只读取规范回收包，不解压用户路径到文件系统。"""
import json
import zipfile

MAX_UPLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024


class Archive:
    def __init__(self, path):
        try:
            self.zip = zipfile.ZipFile(path)
            entries = self.zip.infolist()
            if len(entries) > 1501 or sum(i.file_size for i in entries) > MAX_EXPANDED:
                raise ValueError('回收包解压内容超过 256 MiB 或文件过多')
            if any(i.flag_bits & 1 or i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) for i in entries):
                raise ValueError('请选择未加密的普通 ZIP 回收包')
            self.entries = {i.filename: i for i in entries}
            if len(self.entries) != len(entries):
                raise ValueError('回收包包含重名文件')
            data = self.read('manifest.json')
            if len(data) > 128 * 1024 * 1024:
                raise ValueError('任务清单过大')
            self.manifest = json.loads(data)
            if (not isinstance(self.manifest, dict) or self.manifest.get('format') != 'lulynx-offline-task'
                    or self.manifest.get('version') != 1):
                raise ValueError('请选择客户端导出的网页回收包 ZIP')
            job, identity = self.manifest.get('id'), self.manifest.get('identity')
            if (not isinstance(job, str) or len(job) != 17 or not job.startswith('O')
                    or any(ch not in '0123456789abcdef' for ch in job[1:])
                    or not isinstance(identity, dict)
                    or any(not isinstance(identity.get(k), str) or not identity[k] for k in ('user_id', 'device_id', 'secret'))):
                raise ValueError('回收包离线任务编号或身份格式不正确')
            tasks = self.manifest.get('tasks')
            if not isinstance(tasks, list) or not 0 < len(tasks) <= 500:
                raise ValueError('回收包必须包含 1～500 个分片')
            seen, allowed = set(), {'manifest.json'}
            for task in tasks:
                lid = task.get('lease_id', '') if isinstance(task, dict) else ''
                if (not isinstance(lid, str) or not lid.startswith('L') or len(lid) > 100
                        or any(ch not in '0123456789abcdef' for ch in lid[1:]) or lid in seen):
                    raise ValueError('回收包租约格式错误或重复')
                seen.add(lid)
                allowed.add(lid + '/proof.json')
                candidates = [n for n in self.entries if n.startswith(lid + '/') and n.endswith('.jsonl')
                              and n.count('/') == 1 and n.split('/')[1].startswith('artifacts-')]
                if len(candidates) != 1:
                    raise ValueError('每个分片需要一个规范 JSONL 文件：' + lid)
                allowed.update(candidates)
            if set(self.entries) != allowed:
                raise ValueError('回收包文件缺失或包含不属于任务的文件')
        except Exception:
            if hasattr(self, 'zip'): self.zip.close()
            raise

    def read(self, name):
        try:return self.zip.read(name)
        except (KeyError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ValueError('回收包文件不存在或损坏：' + name) from exc

    def files(self, lid):
        names = [n for n in self.entries if n.startswith(lid + '/')]
        return {n.split('/')[1]: self.read(n) for n in names}

    def close(self):
        self.zip.close()
