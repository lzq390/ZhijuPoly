// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach,beforeEach,expect,it,vi } from "vitest";
import rawFixture from "../../../scripts/fixtures/entitlements.json";
import { PersonalEntitlementsPanel } from "./PersonalEntitlementsPanel";
const moduleGroups = rawFixture.operations.module_groups;
const fixture = rawFixture;
const mocks=vi.hoisted(()=>({auth:vi.fn(),balance:vi.fn(),list:vi.fn(),activity:vi.fn(),operation:vi.fn(),result:vi.fn()}));
vi.mock('../../services/usage',async()=>({...await vi.importActual<typeof import('../../services/usage')>('../../services/usage'),fetchUsageActivityByGroup:mocks.activity}));
vi.mock('../../auth/AuthProvider',()=>({useAuth:mocks.auth}));
vi.mock('../../services/entitlements',()=>({fetchEntitlements:mocks.balance,fetchUsageOperationsByGroup:mocks.list,fetchUsageOperation:mocks.operation,fetchUsageOperationResult:mocks.result}));
beforeEach(()=>{vi.stubGlobal('matchMedia',()=>({matches:true,addEventListener:vi.fn(),removeEventListener:vi.fn()}));mocks.auth.mockReturnValue({session:{account_type:"member"}});mocks.activity.mockReset().mockResolvedValue({mode:"activity_only",items:[],offset:0,limit:25,has_more:false,modules:[],module_groups:moduleGroups,warnings:[]});mocks.balance.mockReset().mockResolvedValue(fixture.entitlements);mocks.list.mockReset().mockResolvedValue(fixture.operations);mocks.operation.mockReset().mockResolvedValue(fixture.operations.items[0]);mocks.result.mockReset().mockResolvedValue(fixture.result);});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
function renderReceipts(element = <PersonalEntitlementsPanel activeTab="operations"/>) { const view=render(element); fireEvent.click(screen.getByRole('button',{name:'计次回执'})); return view; }
it('hides static allowance explanations while preserving all pools and period diagnostics',async()=>{
 render(<PersonalEntitlementsPanel/>);await screen.findByText('当前为示例策略，不代表正式套餐承诺。');
 expect(screen.getAllByRole('article')).toHaveLength(8);
 for(const name of Object.values(fixture.entitlements.policy.pool_labels))expect(screen.getByRole('article',{name})).toBeTruthy();
 for(const text of ['当前为示例策略，不代表正式套餐承诺。','次数余额不改变功能权限',/待确认次数包含在预留中/,/余额不保证可立即提交/,/规格由服务端验证/,/请求、操作和执行尝试是不同口径/])expect(screen.getByText(text).closest('[hidden]')).toBeTruthy();
 expect(screen.getByText(/另有 1 次旧周期预留/).closest('[hidden]')).toBeNull();
 const period=screen.getByText(/^当前月周期/);expect(period.closest('[hidden]')).toBeNull();expect(period.textContent).toContain('2026-10-01 08:00:00 至 2026-11-01 08:00:00');
 for(const text of ['HTTP任务请求：21','操作数：16','执行尝试：17'])expect(screen.getByText(text).closest('[hidden]')).toBeNull();
 expect(screen.getByText(/2026-09-01 08:00:00 至 2026-10-01 08:00:00/).closest('[hidden]')).toBeNull();
});
it('keeps runtime request-observation degradation visible without its static definition',async()=>{
 mocks.balance.mockResolvedValue({...fixture.entitlements,request_observation_degraded:true});render(<PersonalEntitlementsPanel/>);
 expect((await screen.findByText('部分请求观测缺失，诊断总数可能不完整。')).closest('[hidden]')).toBeNull();
 expect(screen.getByText(/请求、操作和执行尝试是不同口径/).closest('[hidden]')).toBeTruthy();
 expect(screen.getByText('HTTP任务请求：21').closest('[hidden]')).toBeNull();
});
it('keeps the allowance disabled state while hiding its explanatory continuation',async()=>{
 mocks.balance.mockResolvedValue({enabled:false,mode:'not_enabled',policy:null,pools:[]});render(<PersonalEntitlementsPanel/>);
 const state=await screen.findByText(/^分类次数尚未启用/);expect(state.closest('[hidden]')).toBeNull();expect(state.getAttribute('role')).toBe('status');
 expect(screen.getByText(/已接入任务可在“操作记录”查看/).closest('[hidden]')).toBeTruthy();
});
it.each(['day','week','month'])('uses server %s period and does not infer balance permission',async period=>{
 mocks.balance.mockResolvedValue({...fixture.entitlements,admission_blocked_by_period:true,policy:{...fixture.entitlements.policy,period}});render(<PersonalEntitlementsPanel/>);
 expect((await screen.findByText(/存在尚未结束的冲突周期/)).closest('[hidden]')).toBeNull();expect(screen.getByText(/余额不保证可立即提交/).closest('[hidden]')).toBeTruthy();
});
it('shows free permissions without treating remaining counts as member access',async()=>{
 mocks.auth.mockReturnValue({session:{account_type:'free'}});render(<PersonalEntitlementsPanel/>);await screen.findByText('免费账号');
 expect(screen.queryByText('会员账号')).toBeNull();
});
it('reads durable operations while admission is off',async()=>{
 mocks.balance.mockResolvedValue({enabled:false,mode:'not_enabled',policy:null,pools:[]});renderReceipts();await screen.findByText(/分类次数尚未启用/);
 expect(screen.getByRole('status').textContent).toContain('计次操作记录当前未启用');
 expect(screen.getByText(/新操作不会生成计次回执/).closest('[hidden]')).toBeTruthy();
 expect(mocks.list).toHaveBeenCalledOnce();await screen.findAllByText('读取已有结果');
});
it('hides receipt explanations without hiding its table or pagination',async()=>{
 renderReceipts();await screen.findByRole('region',{name:'操作回执表，可横向滚动'});
 const panel=within(document.getElementById('usage-source-receipts')!);
 for(const text of [/这里展示已生成的计次操作回执/,/部分历史或公共记录没有独立模块归属/,/断线或结果待确认时/])expect(panel.getByText(text).closest('[hidden]')).toBeTruthy();
 expect(screen.getByRole('navigation',{name:'操作回执分页'})).toBeTruthy();
 expect(screen.getAllByRole('button',{name:'查看回执'})).toHaveLength(2);
});
it('refreshes a finished operation and newly created receipts without reading results',async()=>{
 const running={...fixture.operations.items[0],state:'running'};
 mocks.list.mockResolvedValueOnce({...fixture.operations,items:[running],has_more:false});
 const view=renderReceipts();
 const table=within(await screen.findByRole('region',{name:'操作回执表，可横向滚动'}));
 expect(table.getByText('处理中')).toBeTruthy();
 const finished={...running,state:'succeeded'};
 const next={...running,operation_id:'99999999-0000-4000-8000-000000000099',state:'reserved'};
 mocks.list.mockResolvedValue({...fixture.operations,items:[next,finished],has_more:false});
 view.rerender(<PersonalEntitlementsPanel activeTab="operations" refreshVersion={1}/>);
 await table.findByText('已计成功次数');
 expect(table.getByText('已预留')).toBeTruthy();expect(table.queryByText('处理中')).toBeNull();
 expect(table.getByText('99999999…0099')).toBeTruthy();
 expect(mocks.operation).not.toHaveBeenCalled();expect(mocks.result).not.toHaveBeenCalled();
});
it('recovers a selected saved result via reads only',async()=>{
 renderReceipts();await screen.findAllByRole('button',{name:'读取已有结果'});fireEvent.click(screen.getAllByRole('button',{name:'读取已有结果'})[0]);
 expect((await screen.findByText('已有结果已读取，未重新执行任务。')).closest('[hidden]')).toBeNull();expect(mocks.operation.mock.calls[0][0]).toBe(fixture.operations.items[0].operation_id);expect(mocks.result).toHaveBeenCalledOnce();
 expect(within(screen.getByLabelText('已恢复操作结果')).getByText(/synthetic saved result/).closest('[hidden]')).toBeNull();
 expect(screen.getByText(/这里只读取已有回执和结果/).closest('[hidden]')).toBeTruthy();
 expect(screen.getByText(/结果待确认时，预留仍保留/).closest('[hidden]')).toBeTruthy();
});
it('keeps unknown receipt visible after pending result and does not automatically retry',async()=>{
 mocks.operation.mockResolvedValue(fixture.operations.items[1]);mocks.result.mockRejectedValue(new Error('结果待确认，不要重复计算。'));renderReceipts();await screen.findAllByRole('button',{name:'读取已有结果'});fireEvent.click(screen.getAllByRole('button',{name:'读取已有结果'})[1]);
 expect((await screen.findByRole('alert')).closest('[hidden]')).toBeNull();expect(mocks.result).toHaveBeenCalledOnce();expect(screen.getByLabelText('所选操作回执').textContent).toContain('结果待确认');
});
it('does not expose a late old-owner result after the identity subtree unmounts',async()=>{
 let finish!:(value:unknown)=>void;mocks.result.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
 const view=renderReceipts(<PersonalEntitlementsPanel key="a" activeTab="operations"/>);await screen.findAllByRole('button',{name:'读取已有结果'});fireEvent.click(screen.getAllByRole('button',{name:'读取已有结果'})[0]);await waitFor(()=>expect(mocks.result).toHaveBeenCalledOnce());
 mocks.list.mockResolvedValue({items:[],offset:0,limit:25,has_more:false});view.rerender(<PersonalEntitlementsPanel key="b" activeTab="operations"/>);fireEvent.click(screen.getByRole("button",{name:"计次回执"}));await screen.findByText('暂无操作回执。');
 await act(async()=>finish(fixture.result));expect(screen.queryByText(/synthetic saved result/)).toBeNull();
});

it('counts unknown within reserved progress and avoids percentages for unlimited/zero pools',async()=>{
 const pools=[{...fixture.entitlements.pools[0],limit:10,success_used:3,reserved:2,unknown:1,remaining:5},{...fixture.entitlements.pools[1],limit:null,remaining:null},{...fixture.entitlements.pools[2],limit:0,remaining:0}];
 mocks.balance.mockResolvedValue({...fixture.entitlements,pools});render(<PersonalEntitlementsPanel/>);
 const meter=await screen.findByRole('meter');expect(meter.getAttribute('aria-valuenow')).toBe('5');expect(meter.getAttribute('aria-valuemax')).toBe('10');
 expect(screen.getAllByRole('meter')).toHaveLength(1);expect(screen.getByText('周期上限为 0 次')).toBeTruthy();expect(screen.getAllByText('未设次数上限').length).toBeGreaterThan(0);
});

it('keeps tabpanels mounted and preserves operation filters and page across tab changes',async()=>{
 mocks.list.mockImplementation((offset,module)=>Promise.resolve({...fixture.operations,offset,has_more:offset===0,items:fixture.operations.items.map(item=>({...item,module:module||item.module}))}));
 const view=renderReceipts();
 await screen.findAllByRole('button',{name:'查看回执'});expect(document.getElementById('usage-panel-overview')?.hidden).toBe(true);
 fireEvent.click(screen.getByRole('combobox',{name:'功能模块'}));fireEvent.click(await screen.findByRole('option',{name:'均聚物性质预测'}));
 await waitFor(()=>expect(mocks.list.mock.calls.at(-1)?.[1]).toBe('homopolymerPrediction'));
 await waitFor(()=>expect(screen.getByRole('button',{name:'下一页操作'}).hasAttribute('disabled')).toBe(false));
 fireEvent.click(screen.getByRole('button',{name:'下一页操作'}));await screen.findByText('第 2 页');
 await waitFor(()=>expect(mocks.list.mock.calls.at(-1)?.[0]).toBe(25));
 view.rerender(<PersonalEntitlementsPanel activeTab="overview"/>);expect(document.getElementById('usage-panel-operations')?.hidden).toBe(true);
 view.rerender(<PersonalEntitlementsPanel activeTab="operations"/>);expect(screen.getByText('第 2 页')).toBeTruthy();expect(screen.getByRole('combobox',{name:'功能模块'}).textContent).toContain('均聚物性质预测');
 expect(mocks.list).toHaveBeenCalledTimes(3);
});

it('refreshes both sources while preserving successful cards',async()=>{
 const busy=vi.fn();const view=render(<PersonalEntitlementsPanel onBusyChange={busy}/>);await screen.findAllByRole('article');
 let finish!:(value:unknown)=>void;mocks.balance.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
 view.rerender(<PersonalEntitlementsPanel refreshVersion={1} onBusyChange={busy}/>);await screen.findByText('分类次数更新中…');expect(screen.getAllByRole('article')).toHaveLength(8);
 await waitFor(()=>expect(mocks.list).toHaveBeenCalledTimes(2));expect(busy.mock.calls.at(-1)?.[0]).toBe(true);
 await act(async()=>finish(fixture.entitlements));await waitFor(()=>expect(busy.mock.calls.at(-1)?.[0]).toBe(false));
});

it('opens a receipt without reading results and allows a manual result read',async()=>{
 renderReceipts();fireEvent.click((await screen.findAllByRole('button',{name:'查看回执'}))[0]);
 const drawer=within(await screen.findByRole('dialog',{name:'操作详情'}));await drawer.findByText(fixture.operations.items[0].operation_id,{exact:true});
 expect(mocks.result).not.toHaveBeenCalled();fireEvent.click(drawer.getByRole('button',{name:'读取已有结果'}));await drawer.findByText('已有结果已读取，未重新执行任务。');expect(mocks.result).toHaveBeenCalledOnce();
});

it('copies the full operation ID and reports a clipboard failure',async()=>{
 const writeText=vi.fn().mockResolvedValue(undefined);Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText}});
 renderReceipts();fireEvent.click((await screen.findAllByRole('button',{name:'查看回执'}))[0]);
 fireEvent.click(await screen.findByRole('button',{name:'复制操作 ID'}));expect((await screen.findByText('操作 ID 已复制')).closest('[hidden]')).toBeNull();expect(writeText).toHaveBeenCalledWith(fixture.operations.items[0].operation_id);
 writeText.mockRejectedValueOnce(new Error('clipboard denied'));fireEvent.click(screen.getByRole('button',{name:'复制操作 ID'}));expect((await screen.findByText('复制失败，请选中完整操作 ID 手动复制。')).closest('[hidden]')).toBeNull();
});

it('traps Tab, closes on Escape and restores trigger focus',async()=>{
 renderReceipts();const trigger=(await screen.findAllByRole('button',{name:'查看回执'}))[0];trigger.focus();fireEvent.click(trigger);
 const drawer=within(await screen.findByRole('dialog'));const close=drawer.getByRole('button',{name:'关闭操作详情'});await waitFor(()=>expect(document.activeElement).toBe(close));
 await drawer.findByRole('button',{name:'读取已有结果'});fireEvent.keyDown(close,{key:'Tab',shiftKey:true});expect(document.activeElement).toBe(drawer.getByRole('button',{name:'读取已有结果'}));
 fireEvent.keyDown(document,{key:'Escape'});await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());await waitFor(()=>expect(document.activeElement).toBe(trigger));
});

it.each(['close','tab','source'])('aborts and hides a late saved result after %s',async action=>{
 let finish!:(value:unknown)=>void;mocks.result.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));const view=renderReceipts();
 fireEvent.click((await screen.findAllByRole('button',{name:'读取已有结果'}))[0]);await waitFor(()=>expect(mocks.result).toHaveBeenCalledOnce());const signal=mocks.result.mock.calls[0][1] as AbortSignal;
 if(action==='close')fireEvent.click(screen.getByRole('button',{name:'关闭操作详情'}));else if(action==='source')fireEvent.click(screen.getByRole('button',{name:'任务记录',hidden:true}));else view.rerender(<PersonalEntitlementsPanel activeTab="overview"/>);
 expect(signal.aborted).toBe(true);await act(async()=>finish(fixture.result));expect(screen.queryByText(/synthetic saved result/)).toBeNull();
});

it('preserves successful cards on refresh failure and retries only the failed source',async()=>{
 const view=render(<PersonalEntitlementsPanel/>);await screen.findAllByRole('article');mocks.balance.mockRejectedValueOnce(new Error('次数服务暂时不可用'));
 view.rerender(<PersonalEntitlementsPanel refreshVersion={1}/>);await screen.findByText('更新失败，当前显示上次成功读取的数据。次数服务暂时不可用');expect(screen.getAllByRole('article')).toHaveLength(8);
 expect(mocks.list).toHaveBeenCalledTimes(2);fireEvent.click(screen.getByRole('button',{name:'重试'}));await waitFor(()=>expect(mocks.balance).toHaveBeenCalledTimes(3));expect(mocks.list).toHaveBeenCalledTimes(2);
});

it('does not leak the previous operation result into a newly opened receipt',async()=>{
 let finish!:(value:unknown)=>void;mocks.result.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));
 mocks.operation.mockImplementation(id=>Promise.resolve(fixture.operations.items.find(item=>item.operation_id===id)));
 renderReceipts();fireEvent.click((await screen.findAllByRole('button',{name:'读取已有结果'}))[0]);await waitFor(()=>expect(mocks.result).toHaveBeenCalledOnce());
 fireEvent.click(screen.getByRole('button',{name:'关闭操作详情'}));await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull());
 fireEvent.click(screen.getAllByRole('button',{name:'查看回执'})[1]);await within(screen.getByRole('dialog')).findByText(fixture.operations.items[1].operation_id,{exact:true});
 await act(async()=>finish(fixture.result));expect(screen.queryByText(/synthetic saved result/)).toBeNull();expect(mocks.result).toHaveBeenCalledOnce();
});

it('portals outside the scrolling container while retaining the page visibility boundary',async()=>{
 renderReceipts(<section className="np-usage-page"><div className="np-usage-body"><div className="np-usage-content"><PersonalEntitlementsPanel activeTab="operations"/></div></div></section>);
 fireEvent.click((await screen.findAllByRole('button',{name:'查看回执'}))[0]);const dialog=await screen.findByRole('dialog');
 expect(dialog.closest('.np-usage-content')).toBeNull();expect(dialog.closest('.np-usage-body')).toBeNull();expect(dialog.closest('.np-usage-page')).toBeTruthy();
});

it('uses the sidebar MD group while retaining distinct polymer and demo receipt types',async()=>{
 const items=fixture.operations.items.map((item,index)=>({...item,module:index?'md_demo':'polymer_md'}));
 mocks.list.mockResolvedValue({...fixture.operations,items});
 mocks.operation.mockResolvedValue(items[0]);
 renderReceipts();await screen.findAllByRole('button',{name:'查看回执'});
 fireEvent.click(screen.getByRole('combobox',{name:'功能模块'}));
 expect(screen.getAllByRole('option').map(option=>option.textContent)).toEqual(['全部模块',...moduleGroups.map(group=>group.label)]);
 expect(screen.getByRole('option',{name:'单体 MD 模拟'})).toBeTruthy();expect(screen.queryByRole('option',{name:'分子动力学演示'})).toBeNull();
 fireEvent.click(screen.getByRole('option',{name:'MD 模拟'}));
 await waitFor(()=>expect(mocks.list.mock.calls.at(-1)?.slice(0,2)).toEqual([0,'mdSimulationDemo']));
 const table=within(await screen.findByRole('region',{name:'操作回执表，可横向滚动'}));
 expect(table.getByText('聚合物分子动力学')).toBeTruthy();expect(table.getByText('分子动力学演示')).toBeTruthy();
 fireEvent.click(table.getAllByRole('button',{name:'查看回执'})[0]);
 const detail=within(await screen.findByRole('dialog',{name:'操作详情'}));
 await detail.findByText('聚合物分子动力学');expect(detail.getByRole('heading',{name:'MD 模拟'})).toBeTruthy();expect(mocks.result).not.toHaveBeenCalled();
});
