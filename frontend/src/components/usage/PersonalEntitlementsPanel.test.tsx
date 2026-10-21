// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach,beforeEach,expect,it,vi } from "vitest";
import fixture from "../../../scripts/fixtures/entitlements.json";
import { PersonalEntitlementsPanel } from "./PersonalEntitlementsPanel";
const mocks=vi.hoisted(()=>({auth:vi.fn(),balance:vi.fn(),list:vi.fn(),operation:vi.fn(),result:vi.fn()}));
vi.mock('../../auth/AuthProvider',()=>({useAuth:mocks.auth}));
vi.mock('../../services/entitlements',()=>({fetchEntitlements:mocks.balance,fetchUsageOperations:mocks.list,fetchUsageOperation:mocks.operation,fetchUsageOperationResult:mocks.result}));
beforeEach(()=>{mocks.auth.mockReturnValue({session:{account_type:"member"}});mocks.balance.mockReset().mockResolvedValue(fixture.entitlements);mocks.list.mockReset().mockResolvedValue(fixture.operations);mocks.operation.mockReset().mockResolvedValue(fixture.operations.items[0]);mocks.result.mockReset().mockResolvedValue(fixture.result);});
afterEach(cleanup);
it('shows all eight pools, sample policy and separate reservations/request diagnostics',async()=>{
 render(<PersonalEntitlementsPanel/>);await screen.findByText('当前为示例策略，不代表正式套餐承诺。');
 expect(screen.getAllByRole('article')).toHaveLength(8);
 for(const name of Object.values(fixture.entitlements.policy.pool_labels))expect(screen.getByRole('article',{name})).toBeTruthy();
 expect(screen.getByText(/待确认次数包含在预留中/)).toBeTruthy();expect(screen.getByText(/另有 1 次旧周期预留/)).toBeTruthy();
 expect(screen.getByText('HTTP任务请求：21')).toBeTruthy();expect(screen.getByText('操作数：16')).toBeTruthy();expect(screen.getByText('执行尝试：17')).toBeTruthy();
});
it.each(['day','week','month'])('uses server %s period and does not infer balance permission',async period=>{
 mocks.balance.mockResolvedValue({...fixture.entitlements,admission_blocked_by_period:true,policy:{...fixture.entitlements.policy,period}});render(<PersonalEntitlementsPanel/>);
 await screen.findByText(/存在尚未结束的冲突周期/);expect(screen.getByText(/余额不保证可立即提交/)).toBeTruthy();
});
it('shows free permissions without treating remaining counts as member access',async()=>{
 mocks.auth.mockReturnValue({session:{account_type:'free'}});render(<PersonalEntitlementsPanel/>);await screen.findByText(/免费账号：会员服务尚未开放/);
 expect(screen.queryByText(/会员账号的成功次数/)).toBeNull();
});
it('reads durable operations while admission is off',async()=>{
 mocks.balance.mockResolvedValue({enabled:false,mode:'not_enabled',policy:null,pools:[]});render(<PersonalEntitlementsPanel/>);await screen.findByText(/分类次数尚未启用/);
 expect(mocks.list).toHaveBeenCalledOnce();await screen.findAllByText('读取已有结果');
});
it('recovers a selected saved result via reads only',async()=>{
 render(<PersonalEntitlementsPanel/>);await screen.findAllByRole('button',{name:'读取已有结果'});fireEvent.click(screen.getAllByRole('button',{name:'读取已有结果'})[0]);
 await screen.findByText('已有结果已读取，未重新执行任务。');expect(mocks.operation.mock.calls[0][0]).toBe(fixture.operations.items[0].operation_id);expect(mocks.result).toHaveBeenCalledOnce();
 expect(within(screen.getByLabelText('已恢复操作结果')).getByText(/synthetic saved result/)).toBeTruthy();
});
it('keeps unknown receipt visible after pending result and does not automatically retry',async()=>{
 mocks.operation.mockResolvedValue(fixture.operations.items[1]);mocks.result.mockRejectedValue(new Error('结果待确认，不要重复计算。'));render(<PersonalEntitlementsPanel/>);await screen.findAllByRole('button',{name:'读取已有结果'});fireEvent.click(screen.getAllByRole('button',{name:'读取已有结果'})[1]);
 await screen.findByRole('alert');expect(mocks.result).toHaveBeenCalledOnce();expect(screen.getByLabelText('所选操作回执').textContent).toContain('结果待确认');
});
it('does not expose a late old-owner result after the identity subtree unmounts',async()=>{
 let finish!:(value:unknown)=>void;mocks.result.mockImplementation(()=>new Promise(resolve=>{finish=resolve;}));
 const view=render(<PersonalEntitlementsPanel key="a"/>);await screen.findAllByRole('button',{name:'读取已有结果'});fireEvent.click(screen.getAllByRole('button',{name:'读取已有结果'})[0]);await waitFor(()=>expect(mocks.result).toHaveBeenCalledOnce());
 mocks.list.mockResolvedValue({items:[],offset:0,limit:25,has_more:false});view.rerender(<PersonalEntitlementsPanel key="b"/>);await screen.findByText('暂无操作回执。');
 await act(async()=>finish(fixture.result));expect(screen.queryByText(/synthetic saved result/)).toBeNull();
});
