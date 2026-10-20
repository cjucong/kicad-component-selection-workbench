"""Installed-plugin release updates, outside engineering projects and CAD files."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

VERSION = '0.1.5'
REPOSITORY = 'cjucong/kicad-component-selection-workbench'
IDENTIFIER = 'com.smu.part-selection'
MAX_DOWNLOAD = 40 * 1024 * 1024
RATE_LIMIT_MESSAGE = 'GitHub 暂时拒绝请求或已达到访问频率限制，请稍后重试；当前版本不受影响'


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
        raise ValueError('发布版本号格式无效')
    return tuple(map(int, value.removeprefix('v').split('.')))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PluginUpdater:
    def __init__(self, target, home=None):
        self.target = Path(target).absolute()
        base = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / '.cache'))) / 'KiCadPartSelection'
        key = hashlib.sha256(os.path.normcase(str(self.target)).encode()).hexdigest()[:12]
        self.home = (Path(home) if home else base / 'plugin-updates' / key).absolute()
        self.lock = threading.Lock()
        self.offer = None
        self.pending = None
        self.last_error = ''
        self.last_checked = ''
        self._retry_until = 0
        self.current = VERSION
        marker = self.target / 'build-info.json'
        if marker.is_file():
            self.current = json.loads(marker.read_text(encoding='utf-8'))['version']
            version_tuple(self.current)
        self.installed = (self.target.name == IDENTIFIER.replace('.', '_') and marker.is_file()
                          and not self.target.is_symlink() and not (self.target / '.git').exists())

    def _preferences(self):
        path = self.home / 'preferences.json'
        try:
            mode = json.loads(path.read_text(encoding='utf-8'))['mode']
            return mode if mode in ('off', 'check', 'auto') else 'check'
        except (OSError, ValueError, KeyError, TypeError):
            return 'check'

    def preferences(self, mode):
        if mode not in ('off', 'check', 'auto'):
            raise ValueError('不支持的更新模式')
        with self.lock:
            atomic_json(self.home / 'preferences.json', {'mode':mode})
        return self.status()

    def status(self):
        retry = self._retry_seconds()
        return {'ok':True, 'installed':self.installed, 'current_version':self.current,
                'mode':self._preferences(), 'available':bool(self.offer),
                'latest_version':self.offer['version'] if self.offer else '',
                'notes':self.offer['notes'] if self.offer else '',
                'release_url':self.offer['release_url'] if self.offer else '',
                'ready':bool(self.pending), 'last_error':self.last_error or (RATE_LIMIT_MESSAGE if retry else ''),
                'last_checked':self.last_checked, 'retry_after_seconds':retry}

    def _retry_seconds(self):
        if not self._retry_until:
            try:
                with (self.home / 'rate-limit.json').open(encoding='utf-8') as stream:
                    value = json.loads(stream.read(4096))['retry_at']
                if type(value) in (int, float) and math.isfinite(value):
                    self._retry_until = value
            except (OSError, ValueError, KeyError, TypeError, OverflowError):
                pass
        remaining = self._retry_until - time.time()
        return math.ceil(remaining) if 0 < remaining <= 3600 else 0

    def _remember_rate_limit(self, headers):
        wait = 60
        for key in ('Retry-After', 'X-RateLimit-Reset'):
            try:
                value = int((headers or {}).get(key, ''))
                wait = max(wait, value if key == 'Retry-After' else value - time.time())
            except (ValueError, TypeError, OverflowError):
                pass
        self._retry_until = time.time() + min(3600, wait)
        try:
            atomic_json(self.home / 'rate-limit.json', {'retry_at':self._retry_until})
        except OSError:
            pass  # Keep this session's cooldown even if the external cache is read-only.

    def _request(self, url, limit):
        request = Request(url, headers={'Accept':'application/octet-stream' if url.endswith('.zip') else 'application/json',
            'User-Agent':'KiCad-Component-Selection-Workbench/'+VERSION})
        with urlopen(request, timeout=20) as response:
            final = urlsplit(response.geturl())
            if final.scheme != 'https' or final.hostname not in (
                    'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                    'objects.githubusercontent.com'):
                raise ValueError('更新响应重定向到未认可的下载地址')
            data = response.read(limit+1)
        if len(data)>limit:
            raise ValueError('更新数据超过大小限制')
        return data

    def _json(self, url):
        return json.loads(self._request(url, 2 * 1024 * 1024))

    def _latest_release(self):
        try:
            release = self._json(f'https://github.com/{REPOSITORY}/releases/latest/download/workbench-update.json')
        except HTTPError as exc:
            if exc.code != 404:
                raise
            # Older releases predate the published manifest. Never skip validation.
            return self._json(f'https://api.github.com/repos/{REPOSITORY}/releases/latest')
        if not isinstance(release, dict) or release.get('identifier') != IDENTIFIER:
            raise ValueError('更新清单插件标识不匹配')
        return release

    def _cached_release(self, force):
        try:
            with (self.home / 'release-cache.json').open(encoding='utf-8') as stream:
                data = json.loads(stream.read(2 * 1024 * 1024))
            age = time.time() - data['checked_at']
            if 0 <= age < (60 if force else 6 * 60 * 60) and isinstance(data['release'], dict):
                return data
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def check(self, force=True):
        if type(force) is not bool:
            raise ValueError('检查更新参数无效')
        with self.lock:
            retry = self._retry_seconds()
            if retry:
                self.last_error = RATE_LIMIT_MESSAGE
                raise ValueError(f'{RATE_LIMIT_MESSAGE}；约 {retry} 秒后可重试')
            self.last_error = ''
            previous_offer, previous_pending = self.offer, self.pending
            self.offer = None
            try:
                cached = self._cached_release(force)
                try:
                    release = cached['release'] if cached else self._latest_release()
                except HTTPError as exc:
                    if exc.code in (403, 429):
                        self._remember_rate_limit(exc.headers)
                        raise ValueError(RATE_LIMIT_MESSAGE) from exc
                    if exc.code != 404:
                        raise
                    self.offer = None
                    self.pending = None
                    self.last_checked = datetime.now(timezone.utc).isoformat()
                    return {**self.status(), 'message':'尚无正式发布版本；当前安装可继续使用'}
                version = release.get('tag_name', '').removeprefix('v')
                newer = version_tuple(version)>version_tuple(self.current)
                if release.get('draft') or release.get('prerelease') or not newer:
                    self.offer = None
                else:
                    filename = f'kicad-component-selection-workbench-v{version}.zip'
                    asset = next((a for a in release.get('assets', []) if a.get('name')==filename), None)
                    if not asset:
                        raise ValueError('发布版本尚无可安装 ZIP，请稍后检查')
                    expected = f'https://github.com/{REPOSITORY}/releases/download/v{version}/{filename}'
                    if asset.get('browser_download_url') != expected:
                        raise ValueError('更新下载地址不属于当前插件仓库')
                    digest = asset.get('digest') or ''
                    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
                        raise ValueError('发布包缺少 SHA-256 校验信息，拒绝更新')
                    size = asset.get('size', 0)
                    if type(size) is not int or not 0<size<=MAX_DOWNLOAD:
                        raise ValueError('发布包大小无效')
                    self.offer = {'version':version,'url':expected,'sha256':digest[7:], 'size':size,
                        'notes':str(release.get('body') or '')[:12000],
                        'release_url':f'https://github.com/{REPOSITORY}/releases/tag/v{version}'}
                checked_at = cached['checked_at'] if cached else time.time()
                self.last_checked = datetime.fromtimestamp(checked_at, timezone.utc).isoformat()
                if self.offer != previous_offer:
                    self.pending = None
                else:
                    self.pending = previous_pending
                if not cached:
                    try:
                        atomic_json(self.home / 'release-cache.json', {'checked_at':checked_at, 'release':release})
                    except OSError:
                        pass  # A cache write failure must not misreport a successful network check.
                result = self.status()
                if cached:
                    result['message'] = '使用最近的更新检查结果；启动检查缓存 6 小时，手动检查间隔至少 1 分钟。'
                return result
            except Exception as exc:
                self.offer, self.pending = previous_offer, previous_pending
                self.last_error = str(exc)
                raise

    @contextmanager
    def _installation_lock(self):
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.home / 'installation.lock'
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ValueError('其他窗口正在检查安装状态，请稍后重试；异常退出时可人工核查工程外更新目录') from None
        try:
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(str(os.getpid()))
            yield
        finally:
            path.unlink(missing_ok=True)

    def register(self):
        if not self.installed:
            return
        with self._installation_lock():
            atomic_json(self.home / 'active' / f'{os.getpid()}.json', {'pid':os.getpid()})

    def unregister(self):
        (self.home / 'active' / f'{os.getpid()}.json').unlink(missing_ok=True)

    def _other_sessions(self):
        for path in (self.home / 'active').glob('*.json'):
            pid = int(path.stem)
            if pid == os.getpid():
                continue
            if os.name == 'nt':
                import ctypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
                kernel.OpenProcess.restype = ctypes.c_void_p
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                handle = kernel.OpenProcess(0x00100000, False, pid)
                if handle:
                    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                    kernel.WaitForSingleObject.restype = ctypes.c_uint32
                    try:
                        alive = kernel.WaitForSingleObject(handle, 0) != 0
                    finally:
                        kernel.CloseHandle(handle)
                else:
                    alive = ctypes.get_last_error()!=87
            else:
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            if alive:
                raise ValueError('请先关闭使用同一插件的其他工程工作台，再安装更新')
            path.unlink(missing_ok=True)

    def _archive_files(self, archive):
        files, seen, total = {}, set(), 0
        with zipfile.ZipFile(archive) as package:
            if len(package.infolist())>500:
                raise ValueError('更新包文件数过多')
            for entry in package.infolist():
                name = entry.filename
                parts = PurePosixPath(name).parts
                if ('\\' in name or ':' in name or name.startswith('/') or '..' in parts
                        or PurePosixPath(name).as_posix()!=name.rstrip('/')
                        or any(p.rstrip(' .')!=p or re.match(r'^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', p, re.I)
                               for p in parts) or stat.S_ISLNK(entry.external_attr>>16)):
                    raise ValueError('更新包包含不安全路径')
                if entry.is_dir():
                    continue
                key = name.lower()
                if key in seen:
                    raise ValueError('更新包包含重复文件')
                seen.add(key)
                total += entry.file_size
                if total>MAX_DOWNLOAD:
                    raise ValueError('解压后的更新包过大')
                if not name.startswith('plugins/'):
                    continue
                relative = name.removeprefix('plugins/')
                if not relative or parts[1].lower() in ('.git','third_party','bom','doc','libraries'):
                    raise ValueError('更新包尝试覆盖工程或转换器数据')
                files[relative] = package.read(entry)
        required = {'plugin.json','desktop.py','updater.py','build-info.json',
                    'app/part_selection_server.py','app/part_selection_lifecycle.py'}
        if not required.issubset(files):
            raise ValueError('更新包缺少必要插件文件')
        if json.loads(files['plugin.json'])['identifier']!=IDENTIFIER:
            raise ValueError('更新包插件标识不匹配')
        if json.loads(files['build-info.json'])['version']!=self.offer['version']:
            raise ValueError('更新包版本与发布信息不一致')
        for name, data in files.items():
            if name.endswith('.py'):
                compile(data, name, 'exec')
        return files

    def prepare(self):
        with self.lock, self._installation_lock():
            if not self.installed:
                raise ValueError('源码运行不支持覆盖更新，请更新 Git 仓库后重新安装插件')
            if not self.offer:
                raise ValueError('请先检查更新')
            self._other_sessions()
            self.home.mkdir(parents=True, exist_ok=True)
            path = self.home / 'download.zip'
            try:
                data = self._request(self.offer['url'], MAX_DOWNLOAD)
                if len(data)!=self.offer['size'] or hashlib.sha256(data).hexdigest()!=self.offer['sha256']:
                    raise ValueError('发布包 SHA-256 或大小校验失败，未修改安装')
                path.write_bytes(data)
                files = self._archive_files(path)
                stage = self.home / 'staged'
                if stage.exists():
                    shutil.rmtree(stage)
                stage.mkdir()
                for name, contents in files.items():
                    target = stage / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(contents)
                self.pending = {'stage':stage,'sha256':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
                self.last_error = ''
                return self.status()
            except Exception as exc:
                self.pending = None
                self.last_error = str(exc)
                raise
            finally:
                path.unlink(missing_ok=True)

    def apply(self):
        with self.lock, self._installation_lock():
            if not self.installed or not self.pending:
                raise ValueError('尚无通过校验的更新包')
            self._other_sessions()
            if self.target.is_symlink() or not self.target.is_dir():
                raise ValueError('插件安装目录发生变化，拒绝更新')
            if any(path.is_symlink() for path in self.target.rglob('*')):
                raise ValueError('插件包含链接，拒绝覆盖安装')
            stage = self.pending['stage']
            if stage.is_symlink() or any(path.is_symlink() for path in stage.rglob('*')):
                raise ValueError('暂存更新包包含链接，拒绝安装')
            actual = {path.relative_to(stage).as_posix() for path in stage.rglob('*') if path.is_file()}
            if actual != set(self.pending['sha256']):
                raise ValueError('暂存更新包文件清单已改变，拒绝安装')
            for name, digest in self.pending['sha256'].items():
                path = stage / name
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                    raise ValueError('暂存更新包已改变，拒绝安装')
            # Preserve all converter files byte-for-byte, including notices and source.
            bundled = self.target / 'third_party'
            if bundled.exists():
                shutil.copytree(bundled, stage / 'third_party', dirs_exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            backup = self.home / 'backups' / stamp
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(self.target, backup)
            sibling = self.target.parent / ('.selection-update-'+stamp)
            previous = self.target.parent / ('.selection-previous-'+stamp)
            try:
                shutil.copytree(stage, sibling)
                self.target.rename(previous)
                try:
                    sibling.rename(self.target)
                except Exception:
                    previous.rename(self.target)
                    raise
                self.current = self.offer['version']
                self.pending = None
                self.offer = None
                # Retain the external backup; remove the temporary old installation.
                shutil.rmtree(previous, ignore_errors=True)
                try:
                    atomic_json(self.home / 'last-update.json', {'version':self.current,'backup':str(backup)})
                except OSError:
                    pass  # A missing receipt must not misreport a successfully swapped install.
                return {'ok':True,'version':self.current,'backup':str(backup)}
            finally:
                if sibling.exists():
                    shutil.rmtree(sibling)
