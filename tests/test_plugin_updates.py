"""Release updates only touch a disposable installed plugin, never production CAD."""
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile
import ssl
from urllib.error import URLError
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/kicad_plugin'))
from updater import PluginUpdater, REPOSITORY, VERSION


def release(version='9.0.0'):
    name = f'kicad-component-selection-workbench-v{version}.zip'
    return {'identifier':'com.smu.part-selection', 'tag_name': 'v'+version, 'draft': False, 'prerelease': False,
        'html_url': f'https://github.com/{REPOSITORY}/releases/tag/v{version}',
        'body': 'Verified test release', 'assets': [{'name': name,
        'browser_download_url': f'https://github.com/{REPOSITORY}/releases/download/v{version}/{name}',
        'digest': 'sha256:'+'a'*64, 'size': 100}]}


@pytest.fixture
def installed(scratch_project):
    target = scratch_project / 'plugins/com_smu_part-selection'
    (target / 'app').mkdir(parents=True)
    (target / 'plugin.json').write_text(json.dumps({'identifier':'com.smu.part-selection'}))
    (target / 'desktop.py').write_text('# existing desktop')
    (target / 'build-info.json').write_text(json.dumps({'version':VERSION}))
    return PluginUpdater(target, scratch_project.parent / 'updates')


def test_release_check_reports_a_new_version_and_remembers_preferences(installed, monkeypatch):
    monkeypatch.setattr(installed, '_json', lambda url: release())
    result = installed.check()
    assert result['available'] and result['latest_version']=='9.0.0'
    assert result['current_version']==VERSION and result['mode']=='check'
    installed.preferences('auto')
    assert PluginUpdater(installed.target, installed.home).status()['mode']=='auto'


@pytest.mark.parametrize('version', ['0.0.1', VERSION])
def test_release_check_does_not_offer_downgrades(installed, monkeypatch, version):
    monkeypatch.setattr(installed, '_json', lambda url: release(version))
    result = installed.check()
    assert not result['available']
    assert result['latest_version'] == version
    assert installed.status()['latest_version'] == version


def test_previous_public_install_receives_current_release(installed, monkeypatch):
    (installed.target / 'build-info.json').write_text(json.dumps({'version':'0.1.5'}))
    previous = PluginUpdater(installed.target, installed.home)
    monkeypatch.setattr(previous, '_json', lambda url: release(VERSION))
    result = previous.check()
    assert result['available']
    assert result['current_version'] == '0.1.5'
    assert result['latest_version'] == VERSION


def test_source_checkout_cannot_be_replaced(scratch_project):
    assert not PluginUpdater(scratch_project, scratch_project.parent / 'updates').status()['installed']


def test_tls_eof_retries_once_without_disabling_verification(installed, monkeypatch):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.geturl.return_value = f'https://github.com/{REPOSITORY}/releases/latest/download/workbench-update.json'
    response.read.return_value = b'{}'
    opening = Mock(side_effect=[URLError(ssl.SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING')), response])
    monkeypatch.setattr('updater.urlopen', opening)
    assert installed._request(response.geturl(), 1024) == b'{}'
    assert opening.call_count == 2
    assert all('context' not in call.kwargs for call in opening.call_args_list)


@pytest.mark.parametrize('failure', [ssl.SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING'),
    ssl.SSLCertVerificationError(1, 'CERTIFICATE_VERIFY_FAILED'), TimeoutError('timed out')])
def test_network_failure_is_readable_bounded_and_keeps_install(installed, monkeypatch, failure):
    opening = Mock(side_effect=URLError(failure))
    monkeypatch.setattr('updater.urlopen', opening)
    with pytest.raises(ValueError, match='连接|证书|超时'):
        installed.check()
    assert opening.call_count == (2 if isinstance(failure, ssl.SSLEOFError) else 1)
    assert '当前版本不受影响' in installed.status()['last_error']
    assert 'urlopen error' not in installed.status()['last_error']
    assert not installed.pending and not installed.last_checked
    assert (installed.target / 'desktop.py').read_text() == '# existing desktop'


def test_release_cannot_download_from_an_unrelated_site(installed, monkeypatch):
    data = release()
    data['assets'][0]['browser_download_url']='https://example.com/plugin.zip'
    monkeypatch.setattr(installed, '_json', lambda url: data)
    with pytest.raises(ValueError, match='下载地址'):
        installed.check()


def prepare_release(installed, monkeypatch, extra=None):
    files = {'plugins/plugin.json':json.dumps({'identifier':'com.smu.part-selection'}),
        'plugins/desktop.py':'# updated desktop', 'plugins/updater.py':'# updater',
        'plugins/build-info.json':json.dumps({'version':'9.0.0'}),
        'plugins/app/part_selection_server.py':'# updated server',
        'plugins/app/part_selection_lifecycle.py':'# lifecycle'}
    files.update(extra or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    data = output.getvalue()
    info = release()
    info['assets'][0].update(size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(installed, '_json', lambda url: info)
    monkeypatch.setattr(installed, '_request', lambda url, limit: data)
    installed.check()
    return data


def test_verified_update_keeps_converter_and_backup_and_never_touches_cad(installed, monkeypatch):
    converter = installed.target / 'third_party/EasyKiConverter/runtime/easykiconverter.exe'
    converter.parent.mkdir(parents=True)
    converter.write_bytes(b'preserved converter fixture')
    cad = installed.target.parent.parent / 'unchanged.kicad_sch'
    cad.write_text('unchanged design')
    prepare_release(installed, monkeypatch)
    assert installed.prepare()['ready']
    assert (installed.target / 'desktop.py').read_text()=='# existing desktop'
    result = installed.apply()
    assert (installed.target / 'desktop.py').read_text()=='# updated desktop'
    assert converter.read_bytes()==b'preserved converter fixture'
    assert (Path(result['backup']) / 'desktop.py').read_text()=='# existing desktop'
    assert cad.read_text()=='unchanged design'


@pytest.mark.parametrize('name', ['plugins/../../outside.py','plugins/third_party/overwrite.dll',
    'plugins/BOM/selection.json','plugins/app/evil:stream','plugins/app/CON.py'])
def test_untrusted_archive_cannot_escape_or_replace_data(installed, monkeypatch, name):
    prepare_release(installed, monkeypatch, {name:'invalid'})
    with pytest.raises(ValueError):
        installed.prepare()
    assert (installed.target / 'desktop.py').read_text()=='# existing desktop'


def test_digest_failure_does_not_modify_installation(installed, monkeypatch):
    prepare_release(installed, monkeypatch)
    installed.offer['sha256']='0'*64
    with pytest.raises(ValueError, match='校验失败'):
        installed.prepare()
    assert not installed.status()['ready']


def test_failed_directory_swap_restores_original(installed, monkeypatch):
    prepare_release(installed, monkeypatch)
    installed.prepare()
    original = Path.rename
    def rename(path, destination):
        if path.name.startswith('.selection-update-'):
            raise OSError('simulated publish failure')
        return original(path, destination)
    monkeypatch.setattr(Path, 'rename', rename)
    with pytest.raises(OSError, match='publish failure'):
        installed.apply()
    assert (installed.target / 'desktop.py').read_text()=='# existing desktop'


def test_modified_staged_update_is_rejected(installed, monkeypatch):
    prepare_release(installed, monkeypatch)
    installed.prepare()
    (installed.home / 'staged/desktop.py').write_text('# modified')
    with pytest.raises(ValueError, match='已改变'):
        installed.apply()


def test_other_live_workbench_blocks_installation(installed, monkeypatch):
    import os
    prepare_release(installed, monkeypatch)
    active = installed.home / 'active'
    active.mkdir(parents=True)
    (active / f'{os.getppid()}.json').write_text('{}')
    assert installed.prepare()['ready']
    with pytest.raises(ValueError, match=str(os.getppid())):
        installed.apply()


def test_reused_pid_does_not_block_update(installed, monkeypatch):
    prepare_release(installed, monkeypatch)
    active = installed.home / 'active/98765.json'
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps({'pid':98765, 'started':100.0, 'project':'old-project'}))
    monkeypatch.setattr(installed, '_process_info', lambda pid: (True, 200.0))
    installed.prepare()
    assert installed.apply()['ok']
    assert not active.exists()


def test_legacy_session_with_reused_pid_is_pruned(installed, monkeypatch):
    import os
    prepare_release(installed, monkeypatch)
    active = installed.home / 'active/98765.json'
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps({'pid':98765}))
    os.utime(active, (100,100))
    monkeypatch.setattr(installed, '_process_info', lambda pid: (True, 200.0))
    installed.prepare()
    assert installed.apply()['ok']
    assert not active.exists()


def test_live_session_reports_project_and_pid(installed, monkeypatch):
    prepare_release(installed, monkeypatch)
    active = installed.home / 'active/98765.json'
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps({'pid':98765, 'started':100.0, 'project':'OtherBoard'}))
    monkeypatch.setattr(installed, '_process_info', lambda pid: (True, 100.0))
    assert installed.prepare()['ready']
    with pytest.raises(ValueError, match='OtherBoard.*98765'):
        installed.apply()
    assert active.exists()


def test_unqueryable_session_is_not_silently_ignored(installed, monkeypatch):
    prepare_release(installed, monkeypatch)
    active = installed.home / 'active/98765.json'
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps({'pid':98765, 'started':100.0}))
    monkeypatch.setattr(installed, '_process_info', lambda pid: (None, None))
    installed.prepare()
    with pytest.raises(ValueError, match='98765'):
        installed.apply()
    assert active.exists()


def test_register_records_process_identity_and_project(installed):
    import os
    installed.register(project='ExampleBoard')
    record = installed.home / 'active' / f'{os.getpid()}.json'
    data = json.loads(record.read_text(encoding='utf-8'))
    assert data['project']=='ExampleBoard'
    if os.name == 'nt':
        assert data['started'] is not None
    assert installed._process_info(os.getpid())[0] is True
    installed.unregister()
    assert not record.exists()


def test_dead_process_session_is_pruned(installed, monkeypatch):
    active = installed.home / 'active/98765.json'
    active.parent.mkdir(parents=True)
    active.write_text('{"pid":98765}')
    monkeypatch.setattr(installed, '_process_info', lambda pid: (False, None))
    installed.check_installation_sessions()
    assert not active.exists()


def test_current_workbench_is_not_another_session(installed):
    installed.register(project='CurrentBoard')
    try:
        installed.check_installation_sessions()
    finally:
        installed.unregister()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows process identity integration')
def test_native_process_lifetime_identity_is_queried(installed):
    import os
    import subprocess
    import time
    before = time.time()
    process = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        alive, started = installed._process_info(process.pid)
        # FILETIME and Python's wall clock may differ by a Windows clock tick.
        assert alive is True and before - .1 <= started <= time.time() + .1
        assert installed._process_info(process.pid) == (True, started)
        active = installed.home / 'active' / f'{process.pid}.json'
        active.parent.mkdir(parents=True)
        active.write_text(json.dumps({'pid':process.pid,'started':started,'project':'NativeTestBoard'}))
        with pytest.raises(ValueError, match='NativeTestBoard'):
            installed.check_installation_sessions()
        active.write_text(json.dumps({'pid':process.pid}))
        os.utime(active, (started - 60, started - 60))
        installed.check_installation_sessions()
        assert not active.exists(), 'A process born after a legacy record must not be mistaken for its owner'
        active.write_text(json.dumps({'pid':process.pid,'started':started,'project':'NativeTestBoard'}))
        process.terminate()
        process.wait(timeout=10)
        installed.check_installation_sessions()
        assert not active.exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


def test_extra_staged_file_is_rejected(installed, monkeypatch):
    prepare_release(installed, monkeypatch)
    installed.prepare()
    (installed.home / 'staged/unexpected.py').write_text('# unexpected')
    with pytest.raises(ValueError, match='文件清单已改变'):
        installed.apply()
    assert (installed.target / 'desktop.py').read_text()=='# existing desktop'


def test_download_holds_installation_lock(installed, monkeypatch):
    data = prepare_release(installed, monkeypatch)
    def download(url, limit):
        assert (installed.home / 'installation.lock').is_file()
        return data
    monkeypatch.setattr(installed, '_request', download)
    assert installed.prepare()['ready']
    assert not (installed.home / 'installation.lock').exists()


def test_complete_built_package_can_upgrade_an_older_installation(installed, monkeypatch):
    from package_plugin import build_package, package_files
    archive = build_package(installed.home / 'complete.zip')
    data = archive.read_bytes()
    info = release(VERSION)
    info['assets'][0].update(size=len(data), digest='sha256:'+hashlib.sha256(data).hexdigest())
    installed.current = '0.1.2'
    monkeypatch.setattr(installed, '_json', lambda url: info)
    monkeypatch.setattr(installed, '_request', lambda url, limit: data)
    assert installed.check()['available']
    installed.prepare()
    assert installed.apply()['version']==VERSION
    for name, expected in package_files().items():
        if name.startswith('plugins/'):
            assert (installed.target / name.removeprefix('plugins/')).read_bytes()==expected, name


@pytest.mark.parametrize('code', [403, 429])
def test_rate_limit_is_not_misreported_as_no_release(installed, monkeypatch, code):
    from urllib.error import HTTPError
    def limited(url):
        raise HTTPError(url, code, 'limited', {}, None)
    monkeypatch.setattr(installed, '_json', limited)
    with pytest.raises(ValueError, match='访问频率限制'):
        installed.check()
    assert '访问频率限制' in installed.status()['last_error']
    assert not installed.status()['available']


def test_release_manifest_avoids_the_api_when_its_quota_is_exhausted(installed, monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        assert 'api.github.com' not in url, 'The public API quota is exhausted'
        return release()
    monkeypatch.setattr(installed, '_json', fetch)
    assert installed.check()['available']
    assert calls==[f'https://github.com/{REPOSITORY}/releases/latest/download/workbench-update.json']


def test_successful_check_is_cached_across_workbench_restarts(installed, monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        return release()
    monkeypatch.setattr(installed, '_json', fetch)
    installed.check()
    reopened = PluginUpdater(installed.target, installed.home)
    monkeypatch.setattr(reopened, '_json', lambda url: pytest.fail('Cached startup must not query GitHub'))
    assert reopened.check(force=False)['available']
    assert len(calls)==1


def test_manual_repeated_click_uses_short_cache_then_can_refresh(installed, monkeypatch):
    import updater
    now = [100000.0]
    monkeypatch.setattr(updater.time, 'time', lambda: now[0])
    calls = []
    monkeypatch.setattr(installed, '_json', lambda url: calls.append(url) or release())
    installed.check()
    installed.check(force=True)
    assert len(calls)==1
    now[0] += 61
    installed.check(force=True)
    assert len(calls)==2


def test_manifest_failure_does_not_fall_back_to_unverified_update(installed, monkeypatch):
    info = release()
    info['identifier']='other.plugin'
    monkeypatch.setattr(installed, '_json', lambda url: info)
    with pytest.raises(ValueError, match='标识'):
        installed.check()


def test_old_release_without_manifest_falls_back_to_api(installed, monkeypatch):
    from urllib.error import HTTPError
    calls = []
    def fetch(url):
        calls.append(url)
        if 'api.github.com' not in url:
            raise HTTPError(url,404,'missing',{},None)
        return release()
    monkeypatch.setattr(installed, '_json', fetch)
    assert installed.check()['available']
    assert len(calls)==2 and 'api.github.com' in calls[1]


def test_built_release_manifest_matches_the_actual_archive(installed):
    from package_plugin import build_package, build_update_manifest
    archive = build_package(installed.home / f'kicad-component-selection-workbench-v{VERSION}.zip')
    manifest = json.loads(build_update_manifest(archive).read_text(encoding='utf-8'))
    asset = manifest['assets'][0]
    assert asset['digest']=='sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()
    assert asset['size']==archive.stat().st_size
    assert manifest['identifier']=='com.smu.part-selection'
    assert asset['browser_download_url']==f'https://github.com/{REPOSITORY}/releases/download/v{VERSION}/{archive.name}'


def test_rechecking_keeps_an_already_verified_pending_download(installed, monkeypatch):
    prepare_release(installed, monkeypatch)
    installed.prepare()
    assert installed.check()['ready']
    assert installed.apply()['version']=='9.0.0'


@pytest.mark.parametrize('code', [403, 429])
def test_rate_limit_backoff_survives_restart_without_more_network(installed, monkeypatch, code):
    from urllib.error import HTTPError
    import updater
    now = [100000.0]
    monkeypatch.setattr(updater.time, 'time', lambda: now[0])
    calls = []
    def limited(url):
        calls.append(url)
        raise HTTPError(url, code, 'limited', {'Retry-After':'120'}, None)
    monkeypatch.setattr(installed, '_json', limited)
    with pytest.raises(ValueError, match='访问频率限制'):
        installed.check()
    assert installed.status()['retry_after_seconds'] == 120
    reopened = PluginUpdater(installed.target, installed.home)
    monkeypatch.setattr(reopened, '_json', lambda url: pytest.fail('Cooldown must not query GitHub'))
    for force in (True, False):
        with pytest.raises(ValueError, match='访问频率限制'):
            reopened.check(force=force)
    assert len(calls)==1
    now[0] += 121
    monkeypatch.setattr(reopened, '_json', lambda url: release())
    assert reopened.check()['available']
    assert reopened.status()['retry_after_seconds']==0


@pytest.mark.parametrize('headers,expected', [({},60),({'Retry-After':'invalid'},60),
    ({'Retry-After':'999999'},3600),({'X-RateLimit-Reset':'100090'},90),
    ({'X-RateLimit-Reset':'9'*1000},60)])
def test_rate_limit_wait_is_bounded_and_defensive(installed, monkeypatch, headers, expected):
    from urllib.error import HTTPError
    import updater
    monkeypatch.setattr(updater.time, 'time', lambda:100000.0)
    def limited(url):
        raise HTTPError(url,429,'limited',headers,None)
    monkeypatch.setattr(installed,'_json',limited)
    with pytest.raises(ValueError):
        installed.check()
    assert installed.status()['retry_after_seconds']==expected


def test_failed_recheck_preserves_verified_update_and_cooldown_does_not_block_preferences(installed, monkeypatch):
    from urllib.error import HTTPError
    import updater
    now = [1800000000.0]
    monkeypatch.setattr(updater.time,'time',lambda:now[0])
    prepare_release(installed,monkeypatch)
    installed.prepare()
    previous = installed.pending
    now[0] += 61
    def limited(url):
        raise HTTPError(url,429,'limited',{},None)
    monkeypatch.setattr(installed,'_json',limited)
    with pytest.raises(ValueError):
        installed.check()
    assert installed.pending is previous
    assert installed.status()['ready']
    assert installed.preferences('off')['mode']=='off'
    assert installed.apply()['version']=='9.0.0'


@pytest.mark.parametrize('contents',['not json','null','{"retry_at":"tomorrow"}','{"retry_at":true}',
                                     '{"retry_at":NaN}','{"retry_at":1e100}',
                                     '{"retry_at":'+'9'*1000+'}'])
def test_bad_cooldown_cache_does_not_break_status_or_checks(installed,monkeypatch,contents):
    installed.home.mkdir(parents=True)
    (installed.home / 'rate-limit.json').write_text(contents,encoding='utf-8')
    assert installed.status()['retry_after_seconds']==0
    monkeypatch.setattr(installed,'_json',lambda url:release())
    assert installed.check()['available']
