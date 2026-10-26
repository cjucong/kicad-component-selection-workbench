"""Scoped design synchronization with automatic handoff and explicit risk checks."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import re
import threading
import time
from uuid import uuid4

from part_selection_design_source import DesignSource
from part_selection_footprint_changes import footprint_intent_hint


class DesignSync:
    def __init__(self, app, *, sources=None):
        self.app = app
        self.sources = sources or DesignSource(app)
        self.previews, self.jobs = OrderedDict(), OrderedDict()
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='design-sync')
        self.closed = False

    @staticmethod
    def _payload(payload):
        if not isinstance(payload, dict) or set(payload) - {'refs','destination','alignment','bom'}:
            raise ValueError('同步设计请求无效')
        refs = payload.get('refs')
        if isinstance(refs, str):
            refs = [ref.strip().upper() for ref in refs.split(',') if ref.strip()]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 10000 or any(
                not isinstance(ref, str) or not re.fullmatch(r'[A-Z]+\d+[A-Z]?', ref) for ref in refs
                ) or len(set(refs)) != len(refs):
            raise ValueError('请选择 1 至 10000 个不重复的位号')
        destination, alignment = payload.get('destination'), payload.get('alignment','center')
        if destination not in ('schematic','pcb','both','design') or alignment not in ('rigid','center'):
            raise ValueError('同步目标或对齐方式无效')
        bom = payload.get('bom','__schematic__')
        if not isinstance(bom, str) or len(bom) > 16000:
            raise ValueError('数据来源无效')
        return {'refs':list(refs),'destination':destination,'alignment':alignment,'bom':bom}

    def preview(self, payload, progress=None):
        notify = progress or (lambda *args: None)
        payload = self._payload(payload)
        notify('source', '正在读取原理图与工作台记录')
        with self.app.selection_save_lock:
            source = self.sources.capture(payload['refs'])
            desired = self.app.design_sync_fields(source, payload['bom'])
            if set(desired) != set(payload['refs']):
                raise ValueError('部分器件缺少同步字段，未扩大处理范围')
            changes = [{'ref':ref,'name':name,'before':source['fields'][ref].get(name,''),'after':value}
                       for ref, fields in desired.items() for name, value in fields.items()
                       if source['fields'][ref].get(name, '') != value]
            for change in changes:
                if change['name'] == 'footprint':
                    change.update(kind=footprint_intent_hint(source['records'].get(change['ref'], {}),
                                  change['after']), requires_pcb_verification=True)
        result = {'ok':True,'preview_id':uuid4().hex, **payload, 'schematic_changes':changes,
                  'blocked':False,'pcb_preview':None,'saved':False}
        if payload['destination'] == 'pcb' or (payload['destination'] == 'design' and not changes):
            if changes:
                result.update(blocked=True,suggested_destination='both',
                              reason='所选器件尚有原理图字段未同步，请改为全部同步或先同步原理图')
            else:
                notify('pcb', '正在准备 PCB 实时预检')
                options = {'progress':progress} if progress else {}
                result['pcb_preview'] = self.app.pcb_updates.preview(payload['refs'],payload['bom'],
                    alignment=payload['alignment'],source=source, **options)
                result['blocked'] = bool(result['pcb_preview'].get('blocked'))
        # A PCB-only recheck can belong to an already-authorized automatic design
        # job. Return the same risk decision used by that job's continuation.
        result['pcb_automatic'] = bool(result['pcb_preview']) and self._can_automate(
            {**result, 'destination':'design'})
        if payload['destination'] == 'design':
            result['automatic'] = self._can_automate(result)
        notify('validate', '正在核验预检期间的数据版本')
        if not self.sources.validate(source):
            raise ValueError('预检期间数据已改变，请重新预检')
        with self.lock:
            self._prune()
            self.previews[result['preview_id']] = {'result':deepcopy(result),'source':source,
                'desired':desired,'time':time.monotonic()}
        return result

    def start_preview(self, payload):
        payload = self._payload(payload)
        with self.lock:
            if self.closed:
                raise ValueError('同步服务正在退出')
            self._prune()
            if sum(not job['done'] for job in self.jobs.values()) >= 4:
                raise ValueError('同步任务繁忙，请稍后重试')
            key = uuid4().hex
            self.jobs[key] = {'ok':True, 'job_id':key, 'done':False, 'stage':'queued',
                'message':'等待设计预检', 'needs_confirmation':False, 'phase_results':{},
                '_time':time.monotonic(), '_kind':'preflight',
                'refs':payload['refs'], 'destination':payload['destination']}
            self.jobs[key]['_future'] = self.pool.submit(self._run_preview, key, payload)
            return self.status(key)

    def _run_preview(self, key, payload):
        def progress(stage, message):
            with self.lock:
                if self.jobs[key].get('_cancelled'):
                    raise ValueError('预检已取消')
                self._update(key, stage='preflight:'+stage, message=message)
        try:
            preview = self.preview(payload, progress=progress)
            with self.lock:
                if self.jobs[key].get('_cancelled'):
                    raise ValueError('预检已取消')
                self._update(key, done=True, stage='preview_ready',
                    step_status='blocked' if preview['blocked'] else 'succeeded',
                    message='预检受阻，请核对下方具体原因；未写入设计' if preview['blocked'] else '预检完成', preview=preview)
        except Exception as exc:
            with self.lock:
                cancelled = self.jobs[key].get('_cancelled')
                self._update(key, done=True, ok=bool(cancelled), stage='cancelled' if cancelled else 'failed',
                    message='预检已取消' if cancelled else str(exc), error='' if cancelled else str(exc))

    def cancel_preview(self, key):
        with self.lock:
            job = self.jobs.get(key)
            if not job or job.get('_kind') != 'preflight':
                raise ValueError('仅可取消只读预检，不能中断同步写入')
            if not job['done']:
                job['_cancelled'] = True
                if job['_future'].cancel():
                    self._update(key, done=True, stage='cancelled', message='预检已取消')
                else:
                    self._update(key, stage='cancelling', message='正在取消只读预检')
            return self.status(key)

    def _prune(self):
        for token, state in list(self.previews.items()):
            if not state.get('job_id') and (time.monotonic()-state['time'] > 600 or len(self.previews) >= 32):
                del self.previews[token]
        for key, state in list(self.jobs.items()):
            if state['done'] and state['stage'] not in ('awaiting_pcb_confirmation','partial','uncertain') and (
                    time.monotonic()-state['_time'] > 1800 or len(self.jobs) >= 32):
                del self.jobs[key]

    def _preview(self, token):
        state = self.previews.get(token)
        if not state or time.monotonic()-state['time'] > 600:
            raise ValueError('同步预检已过期，请重新预检')
        return state

    @staticmethod
    def _ack(preview, acknowledgments):
        if not isinstance(acknowledgments, dict) or any(type(value) is not bool for value in acknowledgments.values()):
            raise ValueError('同步确认参数无效')
        if preview.get('blocked'):
            raise ValueError(preview.get('reason') or (preview.get('pcb_preview') or {}).get('reason') or
                             '封装改动类型待核对，请先完成核对')
        items = (preview.get('pcb_preview') or {}).get('items', [])
        ordinary = preview.get('destination') == 'design'
        if any(item.get('needs_ack') and not (ordinary and item.get('selection_verified')) for item in items) and acknowledgments.get('geometry') is not True:
            raise ValueError('请明确确认封装几何替换及引脚功能')
        if any(item.get('max_error_mm',0) > .001 and not (ordinary and item.get('selection_verified')) for item in items) and acknowledgments.get('pad_movement') is not True:
            raise ValueError('请确认焊盘移动，并在更新后检查连接与 DRC')
        drops = [pad for item in items for pad in item.get('dropped_terminals', [])]
        if drops and acknowledgments.get('dropped_terminals') is not True:
            raise ValueError('请明确确认放弃原端子及连接风险')
        if any(pad.get('net_assigned') for pad in drops) and acknowledgments.get('connected_terminals') is not True:
            raise ValueError('放弃已分配网络的端子需要再次确认')

    @classmethod
    def _can_automate(cls, preview):
        try:
            cls._ack(preview, {})
            return True
        except ValueError:
            return False

    def start(self, preview_id, acknowledgments):
        with self.lock:
            state = self.previews.get(preview_id) or {}
            if state.get('job_id'):
                return self.status(state['job_id'])
            if self.closed:
                raise ValueError('同步服务正在退出')
            state = self._preview(preview_id)
            self._ack(state['result'],acknowledgments)
            if not self.sources.validate(state['source']):
                raise ValueError('预检后原理图或工作台记录已改变，请重新预检')
            if any(item.get('dropped_terminals') for item in (state['result'].get('pcb_preview') or {}).get('items', [])):
                self.app.pcb_updates.approve_drops(state['result']['pcb_preview']['preview_id'], acknowledgments)
            if sum(not job['done'] for job in self.jobs.values()) >= 4:
                raise ValueError('同步任务繁忙，请稍后重试')
            key = uuid4().hex
            self.jobs[key] = {'ok':True,'job_id':key,'refs':state['result']['refs'],
                'destination':state['result']['destination'],'alignment':state['result']['alignment'],
                'done':False,'stage':'queued','message':'等待同步设计','needs_confirmation':False,
                'phase_results':{},'schematic_changes':deepcopy(state['result']['schematic_changes']),
                'pcb_preview':deepcopy(state['result']['pcb_preview']),
                '_time':time.monotonic(), '_preview':preview_id}
            state['job_id'] = key
            self.pool.submit(self._run,key,preview_id,False)
            return self.status(key)

    def status(self, key):
        with self.lock:
            state = self.jobs.get(key)
            if state is None:
                return {'ok':False,'done':True,'stage':'expired','error':'同步任务已过期，请只读核验当前设计后重新预检'}
            result = deepcopy({name:value for name,value in state.items() if not name.startswith('_')})
            now = state.get('_finished', time.monotonic())
            result['elapsed_seconds'] = max(0, now-state['_time'])
            result['stage_elapsed_seconds'] = max(0, now-state.get('_stage_started',state['_time']))
            for step in result.get('steps', []):
                if step['status'] in ('running', 'waiting'):
                    step['seconds'] = max(0, now-step.pop('started_at', now))
                else:
                    step.pop('started_at', None)
            return result

    def job_id(self, preview_id):
        with self.lock:
            return self.previews.get(preview_id, {}).get('job_id', '')

    def _update(self, key, **values):
        with self.lock:
            job = self.jobs[key]
            now = time.monotonic()
            explicit_status = values.pop('step_status', None)
            steps = job.setdefault('steps', [])
            next_stage = values.get('stage', job['stage'])
            failed = values.get('ok') is False
            if not steps or next_stage != job['stage']:
                if steps and steps[-1]['status'] in ('running', 'waiting'):
                    steps[-1].update(status='failed' if failed else 'succeeded',
                        seconds=max(0, now-steps[-1]['started_at']))
                steps.append({'stage':next_stage, 'message':values.get('message', job['message']),
                    'status':explicit_status or ('failed' if failed else 'waiting' if next_stage in
                        ('queued', 'awaiting_pcb_confirmation') else 'succeeded' if values.get('done') else 'running'),
                    'started_at':now, 'seconds':0})
                job['steps'] = steps[-512:]
            elif explicit_status or values.get('done'):
                steps[-1].update(status=explicit_status or ('failed' if failed else 'succeeded'),
                    seconds=max(0, now-steps[-1]['started_at']))
            if steps and 'message' in values:
                steps[-1]['message'] = values['message']
            if values.get('stage',job['stage']) != job['stage']:
                job.setdefault('timings', []).append({'stage':job['stage'],
                    'message':job['message'], 'seconds':max(0,now-job.get('_stage_started',job['_time']))})
                job['timings'] = job['timings'][-256:]
                job['_stage_started'] = now
            if values.get('done') is True:
                job['_finished'] = now
            elif values.get('done') is False:
                job.pop('_finished', None)
            job.update(values)

    def _run(self, key, preview_id, pcb_only):
        state = self.previews[preview_id]
        result = state['result']
        automatic = result['destination'] == 'design'
        phase = 'pcb' if pcb_only or result['destination'] == 'pcb' or (
            automatic and not result['schematic_changes']) else 'schematic'
        try:
            if not self.sources.validate(state['source']):
                raise ValueError('排队期间数据已改变，请重新预检')
            if automatic and phase == 'pcb' and not pcb_only:
                self.jobs[key]['phase_results']['schematic'] = {'ok':True,'already_synced':True}
                with self.lock:
                    self.jobs[key]['_pcb_source'] = deepcopy(state['source'])
                self._update(key,stage='schematic_current',step_status='skipped',message='原理图已是最新，不重复写入或关闭')
            if phase == 'schematic':
                self._update(key,stage='schematic',message='正在同步原理图')
                def progress(stage, message):
                    self._update(key,stage='schematic:'+stage,message=message)
                options = {'notify':False} if automatic else {}
                schematic = self.app.bulk_sync_bom_state_to_schematic(result['bom'],
                    selected_refs=result['refs'], prepared_fields=state['desired'],progress=progress,
                    validate_source=lambda:self.sources.refresh_after_save(state['source']), **options)
                if (not schematic.get('ok') or not schematic.get('schematic_verify',{}).get('ok')
                        or schematic.get('missing_refs') or schematic.get('schematic_verify',{}).get('missing_refs')):
                    raise ValueError('原理图写入或回读未通过，未继续 PCB')
                with self.lock:
                    self.jobs[key]['phase_results']['schematic'] = deepcopy(schematic)
                if result['destination'] == 'schematic':
                    if schematic.get('already_synced'):
                        self._update(key, stage='navigation_skipped', step_status='skipped',
                            message='原理图已是最新，跳过保存、关闭和定位')
                    else:
                        phase = 'navigation'
                        self._navigate(key)
                    self._update(key,done=True,stage='complete',message='原理图同步完成',result={'ok':True})
                    return
                phase = 'pcb_preview'
                self._update(key,stage=phase,message='原理图已完成，正在核验当前 PCB')
                source = (self.sources.after_write(state['source'], state['desired']) if automatic
                          else self.sources.capture(result['refs']))
                with self.lock:
                    self.jobs[key]['_pcb_source'] = deepcopy(source)
                pcb = self.app.pcb_updates.preview(result['refs'],result['bom'],alignment=result['alignment'],source=source,
                    progress=lambda stage,message:self._update(key,stage='pcb_preview:'+stage,message=message))
                next_result = {**result, 'pcb_preview':pcb, 'blocked':bool(pcb.get('blocked'))}
                with self.lock:
                    self.jobs[key]['pcb_preview'] = deepcopy(pcb)
                if not automatic or not self._can_automate(next_result):
                    self._update(key,done=True,stage='awaiting_pcb_confirmation',needs_confirmation=True,
                        message='原理图已完成，PCB 存在需要处理的风险' if automatic else '原理图已完成，请核对 PCB 更新后继续',
                        pcb_preview=pcb, pcb_automatic=self._can_automate({**next_result, 'destination':'design'}))
                    return
                result = next_result
            self._update(key,stage='pcb',message='正在更新有差异的 PCB 器件，完成后自动保存核验')
            phase = 'pcb_apply'
            pcb_token = result['pcb_preview']['preview_id']
            options = {'keep_open':True} if automatic else {}
            pcb = self.app.sync_design_pcb(pcb_token,
                progress=lambda stage,message:self._update(key,stage='pcb:'+stage,message=message,
                    step_status='skipped' if stage == 'no_changes' else None), **options)
            schematic = self.jobs[key]['phase_results'].get('schematic', {})
            if automatic and schematic.get('ok') and not schematic.get('already_synced'):
                # The external schematic write deliberately deferred its reopen
                # until all PCB transactions have finished.
                self._update(key,stage='restore_schematic',message='设计已保存，正在恢复原理图')
                try:
                    restored = self.app.notify_kicad_schematic_changed({'updated_refs':result['refs']})
                    if not restored.get('ok'):
                        raise ValueError(restored.get('message') or '原理图恢复未完成')
                except Exception as exc:
                    pcb['navigation_error'] = str(exc) or '原理图恢复未完成'
            if not pcb.get('ok'):
                raise RuntimeError('PCB 发布结果未确认，请只读核验，不要重复执行')
            with self.lock:
                self.jobs[key]['phase_results']['pcb'] = deepcopy(pcb)
            if pcb.get('navigation_error'):
                phase = 'navigation'
                raise ValueError(pcb['navigation_error'])
            self._update(key,done=True,stage='complete',needs_confirmation=False,
                message='PCB 已是最新，无需保存或重开' if pcb.get('no_changes') else
                    '设计同步完成，PCB 已保存核验，保留当前窗口' if automatic else '设计同步完成，PCB 已保存并重新打开',
                result={'ok':True,'saved':bool(pcb.get('saved'))})
        except Exception as exc:
            with self.lock:
                job = self.jobs[key]
                if phase == 'navigation':
                    self._update(key,done=True,ok=False,stage='navigation_failed',
                        message='设计已同步；重新打开或定位未完成：' + str(exc),error=str(exc),
                        result={'ok':False,'retry_stage':'navigation'})
                    return
                uncertain = phase == 'pcb_apply' and not isinstance(exc, ValueError)
                partial = job['phase_results'].get('schematic',{}).get('ok') is True
                if phase == 'schematic':
                    job['phase_results']['schematic'] = {'ok':False,'error':str(exc)}
                else:
                    job['phase_results']['pcb'] = {**getattr(exc, 'pcb_result', {}),
                        'ok':False,'error':str(exc),'uncertain':uncertain}
                self._update(key,done=True,ok=False,needs_confirmation=False,
                    stage='uncertain' if uncertain else 'partial' if partial else 'failed',
                    message=str(exc),error=str(exc),result={'ok':False,
                        'retry_stage':None if uncertain else 'pcb' if phase != 'schematic' else 'schematic'})

    def _navigate(self, key):
        self._update(key, stage='navigation', message='设计已保存，正在重新打开并定位器件')
        result = self.app.reopen_and_locate_design(self.jobs[key]['refs'],
            progress=lambda stage, message:self._update(key, stage='navigation:'+stage, message=message))
        if not result.get('ok'):
            raise ValueError(result.get('message') or '定位未完成')

    def retry_navigation(self, key):
        with self.lock:
            if self.closed:
                raise ValueError('同步服务正在退出')
            job = self.jobs.get(key)
            if not job or job['stage'] != 'navigation_failed' or not job['done']:
                raise ValueError('此任务不能重试定位，请查询当前任务状态')
            if not any(result.get('ok') for result in job['phase_results'].values()):
                raise ValueError('设计尚未核验完成，不能重试定位')
            self._update(key,done=False,ok=True,stage='queued',message='等待重新打开并定位；不重复写入设计')
            self.pool.submit(self._retry_navigation, key)
            return self.status(key)

    def _retry_navigation(self, key):
        try:
            pcb = self.jobs[key]['phase_results'].get('pcb', {})
            if pcb.get('resume_preview_id'):
                recovered = self.app.resume_design_pcb_navigation(pcb['resume_preview_id'],
                    progress=lambda stage,message:self._update(key,stage='navigation:'+stage,message=message))
                self.jobs[key]['phase_results']['pcb'] = deepcopy(recovered)
                if recovered.get('navigation_error'):
                    raise ValueError(recovered['navigation_error'])
            else:
                self._navigate(key)
            with self.lock:
                pcb = self.jobs[key]['phase_results'].get('pcb')
                if pcb and pcb.get('ok'):
                    pcb.pop('navigation_error', None)
                    pcb['located_ref'] = self.jobs[key]['refs'][0]
            self._update(key,done=True,ok=True,stage='complete',message='设计已同步，重新打开及定位完成',result={'ok':True})
        except Exception as exc:
            self._update(key,done=True,ok=False,stage='navigation_failed',message='设计已同步；定位未完成：'+str(exc),
                error=str(exc),result={'ok':False,'retry_stage':'navigation'})

    def continue_pcb(self, job_id, preview_id, acknowledgments):
        with self.lock:
            if self.closed:
                raise ValueError('同步服务正在退出')
            job = self.jobs.get(job_id)
            if not job or job['stage'] not in ('awaiting_pcb_confirmation','partial','pcb','queued'):
                raise ValueError('此任务不能重放 PCB 写入，请只读核验当前 PCB')
            if not job['done']:
                return self.status(job_id)
            if not job['phase_results'].get('schematic',{}).get('ok'):
                raise ValueError('原理图阶段尚未完成')
            approved = deepcopy(job.get('_pcb_source'))
            if not approved or not self.sources.refresh_after_save(approved):
                raise ValueError('原任务核对后的原理图或选料记录已改变，请重新同步设计，不能续写旧任务 PCB')
            if preview_id == (job.get('pcb_preview') or {}).get('preview_id'):
                pcb = job['pcb_preview']
                result = deepcopy(self.previews[job['_preview']]['result'])
                result.update(destination='design' if job['destination'] == 'design' else 'pcb',
                              pcb_preview=pcb,blocked=bool(pcb.get('blocked')))
                state = {'result':result,'source':job['_pcb_source'],
                         'desired':{},'time':time.monotonic()}
                token = uuid4().hex
                self.previews[token] = state
            else:
                token = preview_id
                state = self._preview(token)
                allowed = ('pcb','design') if job['destination'] == 'design' else ('pcb',)
                if (state['result']['destination'] not in allowed or state['result']['refs'] != job['refs']
                        or state.get('job_id') or state['result']['schematic_changes']
                        or not state['result']['pcb_preview']):
                    raise ValueError('PCB 重试预检必须属于同一组器件且尚未使用')
                if job['destination'] == 'design':
                    state['result']['destination'] = 'design'
            self._ack(state['result'],acknowledgments)
            if not self.sources.validate(state['source']):
                raise ValueError('PCB 重试预检已失效')
            if any(item.get('dropped_terminals') for item in (state['result'].get('pcb_preview') or {}).get('items', [])):
                self.app.pcb_updates.approve_drops(state['result']['pcb_preview']['preview_id'], acknowledgments)
            state['job_id'] = job_id
            self._update(job_id,done=False,ok=True,stage='queued',needs_confirmation=False,
                pcb_preview=deepcopy(state['result']['pcb_preview']),message='等待继续 PCB 同步')
            self.pool.submit(self._run,job_id,token,True)
            return self.status(job_id)

    def busy(self):
        with self.lock:
            return any(not state['done'] for state in self.jobs.values())

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True)
