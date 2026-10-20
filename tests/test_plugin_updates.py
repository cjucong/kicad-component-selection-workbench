"""Release updates only touch a disposable installed plugin, never production CAD."""
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

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
    assert not installed.check()['available']


def test_source_checkout_cannot_be_replaced(scratch_project):
    assert not PluginUpdater(scratch_project, scratch_project.parent / 'updates').status()['installed']


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
    with pytest.raises(ValueError, match='其他工程'):
        installed.prepare()


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
