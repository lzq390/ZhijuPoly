import { useEffect, useId, useState, type ReactNode } from "react";
import { Activity, ArrowLeft, ArrowUpRight, Clock3, Cpu, HardDrive, LoaderCircle, LockKeyhole, MemoryStick, RefreshCw, Server, TriangleAlert } from "lucide-react";
import type { ServerMonitoringControl } from "../../hooks/useServerMonitoring";
import type { MonitoringGpu } from "../../types/serverMonitoring";
import { ModulePageHeader } from "../ModulePageHeader";
import { MonitoringTrendChart, monitoringClock, type TrendSeries } from "./MonitoringTrendChart";
import { ResourceInspectionCard } from "./ResourceInspectionCard";
import { WorkbenchSelect } from "../structure-workbench/WorkbenchSelect";
import "./server-monitoring.css";
import "./monitoring-gpu-select.css";

const finite = (value: number | null | undefined): value is number => typeof value === "number" && Number.isFinite(value);
const percent = (value: number | null | undefined) => finite(value) ? `${value.toFixed(1)}%` : "—";
const number = (value: number | null | undefined, suffix = "") => finite(value) ? `${value.toFixed(1)}${suffix}` : "—";
function bytes(value: number | null | undefined) {
  if (!finite(value)) return "—";
  const unit = value >= 2 ** 40 ? "TiB" : value >= 2 ** 30 ? "GiB" : "MiB";
  return `${(value / (unit === "TiB" ? 2 ** 40 : unit === "GiB" ? 2 ** 30 : 2 ** 20)).toFixed(1)} ${unit}`;
}
const hostText = (value: string | null | undefined) => typeof value === "string" && value.trim() ? value.trim() : "--";
const hostCount = (value: number | null | undefined) => finite(value) && Number.isInteger(value) && value > 0 ? String(value) : "--";
const diskAvailable = (value: number | null | undefined) => finite(value) && value >= 0 ? bytes(value) : "--";
function HostInfoValue({ children, note = false }: { children: string; note?: boolean }) {
  return <span className={note ? "sm-host-info-note" : "sm-host-info-value"} title={children}>{children}</span>;
}
function uptime(seconds: number | null | undefined) {
  if (!finite(seconds)) return "运行时长待采集";
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600);
  return days ? `已运行 ${days} 天 ${hours} 小时` : `已运行 ${hours} 小时 ${Math.floor(seconds % 3600 / 60)} 分钟`;
}
function Metric({ label, value, icon, note }: { label: string; value: string; icon: ReactNode; note: string }) {
  return <article className="sm-metric" aria-label={label}><div className="sm-metric-label"><span>{label}</span>{icon}</div><strong>{value}</strong><div className="sm-metric-note" title={note}>{note}</div></article>;
}
const ratio = (used: number | null | undefined, total: number | null | undefined) =>
  finite(used) && finite(total) && total > 0 ? used / total * 100 : null;
const componentLabels = [['cpu', 'CPU'], ['memory', '内存'], ['disks', '磁盘'], ['gpus', 'GPU']] as const;

function useDisplayClock(enabled: boolean) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    if (!enabled) return;
    let timer: ReturnType<typeof setInterval> | undefined;
    const update = () => setNow(Date.now());
    const visibility = () => {
      clearInterval(timer);
      if (document.visibilityState !== "hidden") { update(); timer = setInterval(update, 1000); }
    };
    visibility();
    document.addEventListener("visibilitychange", visibility);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", visibility); };
  }, [enabled]);
  return now;
}

type GpuTaskSelection = { gpuUuid: string | null; gpus: MonitoringGpu[]; onSelectGpu: (uuid: string) => void };

export function ServerMonitoringPage({ control, renderGpuTasks }: { control: ServerMonitoringControl; renderGpuTasks?: (selection: GpuTaskSelection) => ReactNode }) {
  const gpuSelectId = useId();
  const [trend, setTrend] = useState<"system" | "gpu" | "gpuMemory">("system");
  const [selectedGpu, setSelectedGpu] = useState("all");
  const { access, snapshot, history, refresh, refreshing } = control;
  const permitted = access === "allowed";
  const now = useDisplayClock(permitted);
  const age = snapshot ? snapshot.age_seconds + Math.max(0, now - (control.receivedAt ?? now)) / 1000 : null;
  const unavailable = Boolean(snapshot && (snapshot.freshness === "unavailable" || (age ?? 0) > 60));
  const stale = Boolean(snapshot && (snapshot.freshness !== "fresh" || (age ?? 0) > 15 || control.snapshotError));
  const partial = snapshot && Object.values(snapshot.components).some(component => component.status !== "ok");
  const historyStale = history && (history.freshness !== "fresh" || history.age_seconds + Math.max(0, now - (control.historyReceivedAt ?? now)) / 1000 > 90 || control.historyError);
  const gpuCount = snapshot && (snapshot.gpus.length > 0 || snapshot.components.gpus.status === "ok") ? snapshot.gpus.length : null;
  const rootDisk = snapshot?.disks.find(disk => disk.mount_point === "/");
  const dataDisk = snapshot?.disks.find(disk => disk.mount_point === "/data");
  const hostInfo = snapshot?.host_info;
  const gpuMemory = snapshot?.gpus.length && snapshot.gpus.every(gpu => finite(gpu.memory_used_bytes) && finite(gpu.memory_total_bytes) && gpu.memory_total_bytes > 0)
    ? snapshot.gpus.reduce((total, gpu) => ({ used: total.used + gpu.memory_used_bytes!, total: total.total + gpu.memory_total_bytes! }), { used: 0, total: 0 }) : null;
  const componentIssues = snapshot ? componentLabels.filter(([key]) => snapshot.components[key].status !== "ok") : [];
  const selectResource = (uuid: string) => {
    setSelectedGpu(uuid === "server" ? "all" : uuid);
    if (uuid === "server") setTrend("system");
  };
  const colors = ["var(--np-theme-blue)", "var(--np-theme-cyan)", "#8a6ddd", "#da9653", "#cf6f9c", "#5f83a4"];
  const gpuSelection = selectedGpu === "all" || snapshot?.gpus.some(gpu => gpu.uuid === selectedGpu) ? selectedGpu : "all";
  const selectedGpuInfo = snapshot?.gpus.find(gpu => gpu.uuid === gpuSelection);
  const series: TrendSeries[] = trend === "system" ? [
    { key: "cpu", label: "CPU", color: "var(--np-theme-blue)", value: point => point.cpu_percent },
    { key: "memory", label: "内存", color: "var(--np-theme-cyan)", value: point => point.memory_percent }
  ] : (snapshot?.gpus ?? []).map<TrendSeries>((gpu, index) => ({ key: gpu.uuid, label: `GPU ${gpu.index}`, color: colors[index % colors.length], value: point => {
    const sample = point.gpus.find(item => item.uuid === gpu.uuid);
    if (trend === "gpu") return sample?.utilization_percent ?? null;
    return finite(sample?.memory_used_bytes) && finite(gpu.memory_total_bytes) && gpu.memory_total_bytes > 0 ? sample.memory_used_bytes / gpu.memory_total_bytes * 100 : null;
  } })).filter(series => gpuSelection === "all" || series.key === gpuSelection);
  const stateText = access === "disabled" ? "当前入口未启用服务器节点" : access === "guest" ? "登录后查看服务器状态"
    : access === "denied" ? "当前账号暂无访问权限" : access === "unavailable" ? "暂时无法确认访问权限" : "正在确认访问权限";
  return <section className="np-module-page sm-page" data-testid="server-monitoring-page">
    <ModulePageHeader actions={permitted ? <button className="sm-refresh" onClick={refresh} disabled={refreshing} aria-label="刷新服务器节点"><RefreshCw size={14} className={refreshing ? "sm-spin" : ""} />刷新</button> : undefined}>服务器节点</ModulePageHeader>
    {!permitted ? <div className="sm-access" role="status"><div className="sm-access-icon">{access === "checking" ? <LoaderCircle className="sm-spin" /> : <LockKeyhole />}</div><h2>{stateText}</h2><p>{access === "denied" ? "此页面仅向已获授权的账号开放。" : access === "guest" ? "使用已获授权的账号登录，即可查看当前服务器的资源监测。" : access === "unavailable" ? "未展示先前的监测数据，请稍后重试。" : access === "disabled" ? "请使用已启用此功能的开发入口。" : "正在验证当前登录会话。"}</p>
      {access === "guest" && control.login ? <button className="sm-button" onClick={control.login}>登录账号<ArrowUpRight size={15} /></button> : access === "unavailable" ? <button className="sm-button" onClick={refresh}>重新检查<RefreshCw size={14} /></button> : null}
    </div> : <div className="sm-scroll"><div className="sm-content">
      <div className="sm-intro"><span>服务器切换</span></div>
      {(control.snapshotError || stale) && <div className="sm-alert" role="status"><TriangleAlert size={16} /><span>{unavailable ? "采集服务已超过 60 秒未提供有效更新。" : control.snapshotError ?? "采样数据已超过 15 秒有效期。"}{snapshot ? <> 最后更新 <time dateTime={snapshot.captured_at}>{monitoringClock(snapshot.captured_at)}</time>，当前状态尚未确认。</> : " 正在等待采集服务恢复。"}</span></div>}
      {componentIssues.length > 0 && <div className="sm-alert sm-component-alert" role="status"><TriangleAlert size={16} /><span>部分指标暂未就绪：{componentIssues.map(([key, label]) => {
        const component = snapshot!.components[key];
        return `${label}（${component.status === "warming" ? "初始化中" : "暂不可用"}；最后成功 ${component.last_success_at ? monitoringClock(component.last_success_at) : "尚无有效采样"}）`;
      }).join("；")}。</span></div>}
      <div className="sm-inspection-grid sm-server-grid" role="group" aria-label="选择服务器">
        <ResourceInspectionCard kind="server" title="当前服务器" subtitle={snapshot?.hostname ?? "等待硬件信息"} icon={<Server size={19} />} selected onSelect={() => selectResource("server")}
          status={!snapshot ? "等待采样" : stale ? "待更新" : partial ? "部分指标待采集" : "在线"} warning={!snapshot || stale || Boolean(partial)} note={snapshot ? `${snapshot.logical_cpu_count} CPU · ${gpuCount === null ? "GPU 待采集" : `${gpuCount} GPU`}` : "硬件待采集"}
          metrics={[
            { label: "CPU", value: percent(snapshot?.cpu?.utilization_percent), amount: snapshot?.cpu?.utilization_percent ?? null },
            { label: "负载", value: number(snapshot?.load_average?.[0]), amount: snapshot?.load_average?.[0] ?? null, max: snapshot?.logical_cpu_count ?? 1, description: `1 分钟负载；进度条按 ${snapshot?.logical_cpu_count ?? "逻辑"} CPU 数归一化，可超过满刻度` },
            { label: "内存", value: percent(snapshot?.memory?.used_percent), amount: snapshot?.memory?.used_percent ?? null },
            { label: "系统盘", value: percent(rootDisk?.used_percent), amount: rootDisk?.used_percent ?? null },
          ]} />
      </div>
      <section className="sm-detail-panel" aria-label="服务器参数详情">
        <header className="sm-host"><div className="sm-host-main"><div className="sm-server-icon"><Server size={23} /></div><div><div className="sm-host-title"><h2>服务器详情</h2><span className={`sm-status ${!snapshot || stale || partial ? "is-warning" : ""}`}><i />{!snapshot ? "等待采样" : unavailable ? "采集不可用" : stale ? "数据已过期" : partial ? "部分指标待采集" : "在线"}</span></div><div className="sm-host-meta"><span>{snapshot?.hostname ?? "当前服务器"}</span><span>{snapshot ? `${snapshot.logical_cpu_count} 逻辑 CPU` : "硬件信息待采集"}</span><span>{gpuCount === null ? "GPU 数量待采集" : `${gpuCount} 张 GPU`}</span><span>内存 {bytes(snapshot?.memory?.total_bytes)}</span><span>{uptime(snapshot?.uptime_seconds)}</span></div></div></div></header>
        <div className="sm-detail-layout"><div className="sm-metrics">
          <Metric label="CPU 利用率" icon={<Cpu size={16} />} value={percent(snapshot?.cpu?.utilization_percent)} note={snapshot?.components.cpu.status === "warming" ? "正在建立采样基线" : "所有逻辑 CPU 的整体使用率"} />
          <Metric label="内存使用率" icon={<MemoryStick size={16} />} value={percent(snapshot?.memory?.used_percent)} note={`已用 ${bytes(snapshot?.memory?.used_bytes)} / 总量 ${bytes(snapshot?.memory?.total_bytes)}`} />
          <Metric label="系统盘使用率" icon={<HardDrive size={16} />} value={percent(rootDisk?.used_percent)} note={`已用 ${bytes(rootDisk?.used_bytes)} / 总量 ${bytes(rootDisk?.total_bytes)}`} />
          <Metric label="数据盘使用率" icon={<HardDrive size={16} />} value={percent(dataDisk?.used_percent)} note={`已用 ${bytes(dataDisk?.used_bytes)} / 总量 ${bytes(dataDisk?.total_bytes)}`} />
          <Metric label="系统负载 · 1 分钟" icon={<Activity size={16} />} value={number(snapshot?.load_average?.[0])} note={`5 分钟 ${number(snapshot?.load_average?.[1])} · 15 分钟 ${number(snapshot?.load_average?.[2])}`} />
          <Metric label="GPU 总显存占用" icon={<MemoryStick size={16} />} value={percent(ratio(gpuMemory?.used, gpuMemory?.total))} note={gpuMemory ? `已用 ${bytes(gpuMemory.used)} / 总量 ${bytes(gpuMemory.total)}` : "等待全部显卡的有效显存采样"} />
        </div><aside className="sm-host-info" aria-label="主机信息"><h3><Server size={15} />主机信息</h3><dl className="sm-host-info-list">
          <div><dt>CPU</dt><dd><HostInfoValue>{hostText(hostInfo?.cpu_model)}</HostInfoValue><HostInfoValue note>{`${hostCount(hostInfo?.cpu_socket_count)} 插槽 · ${hostCount(hostInfo?.cpu_physical_core_count)} 核 · ${hostCount(snapshot?.logical_cpu_count)} 线程`}</HostInfoValue></dd></div>
          <div><dt>系统</dt><dd><HostInfoValue>{hostText(hostInfo?.os_name)}</HostInfoValue><HostInfoValue note>{hostText(hostInfo?.kernel_release)}</HostInfoValue></dd></div>
          <div><dt>GPU 驱动</dt><dd><HostInfoValue>{hostText(hostInfo?.nvidia_driver_version)}</HostInfoValue></dd></div>
          <div><dt>磁盘可用</dt><dd className="sm-host-disks"><span className="sm-host-disk" title={`系统盘 / 可用 ${diskAvailable(rootDisk?.available_bytes)}`}><span>系统盘</span><strong>{diskAvailable(rootDisk?.available_bytes)}</strong></span><span className="sm-host-disk" title={`数据盘 /data 可用 ${diskAvailable(dataDisk?.available_bytes)}`}><span>数据盘</span><strong>{diskAvailable(dataDisk?.available_bytes)}</strong></span></dd></div>
        </dl></aside></div>
      </section>
      <section className="sm-section sm-gpu-section" aria-label="GPU 监测"><div className="sm-section-heading"><h2><Cpu size={17} />GPU 监测<span>{gpuCount === null ? "数量待采集" : `${gpuCount} 张显卡`}</span></h2></div>
        <div className="sm-inspection-grid sm-gpu-grid" role="group" aria-label="选择趋势显卡卡片">
          {snapshot?.gpus.map(gpu => <ResourceInspectionCard key={gpu.uuid} kind="gpu" title={`GPU ${gpu.index}`} subtitle={gpu.name} icon={<Cpu size={19} />} selected={gpuSelection === gpu.uuid} onSelect={() => selectResource(gpu.uuid)}
            status={stale ? "待更新" : finite(gpu.utilization_percent) ? "在线" : "待采集"} warning={stale || !finite(gpu.utilization_percent)} note={`${bytes(gpu.memory_used_bytes)} / ${bytes(gpu.memory_total_bytes)}`}
            metrics={[
              { label: "利用率", value: percent(gpu.utilization_percent), amount: gpu.utilization_percent },
              { label: "显存", value: percent(ratio(gpu.memory_used_bytes, gpu.memory_total_bytes)), amount: ratio(gpu.memory_used_bytes, gpu.memory_total_bytes) },
              { label: "温度", value: number(gpu.temperature_celsius, " °C"), amount: gpu.temperature_celsius, max: 100, description: "展示刻度 0–100 °C，不代表设备温度上限" },
              { label: "功耗", value: number(gpu.power_watts, " W"), amount: gpu.power_watts, max: 500, description: "展示刻度 0–500 W，不代表设备功率上限" },
            ]} />)}
        </div>{!snapshot?.gpus.length && <div className="sm-small-empty">{gpuCount === 0 ? "当前服务器未检测到 GPU" : "等待显卡硬件与指标采样"}</div>}
      </section>
      <div className="sm-history-layout"><aside className="sm-gpu-tasks" aria-label="GPU 任务"><div className="sm-gpu-tasks-header"><h2><Clock3 size={16} />GPU 任务</h2>{selectedGpuInfo
        ? <button type="button" className="sm-gpu-tasks-scope" aria-label="查看全部 GPU 任务" title="返回全部 GPU" onClick={() => setSelectedGpu("all")}><ArrowLeft size={12} aria-hidden="true" />GPU {selectedGpuInfo.index}</button>
        : <span className="sm-gpu-tasks-scope">全部 GPU</span>}</div><div className="sm-gpu-tasks-body">{renderGpuTasks?.({ gpuUuid: selectedGpuInfo?.uuid ?? null, gpus: snapshot?.gpus ?? [], onSelectGpu: selectResource })}</div></aside>
      <section className="sm-trends"><div className="sm-section-heading"><div><h2><Activity size={17} />资源趋势<span>最近 1 小时</span></h2></div><div className="sm-trend-controls">
        {trend !== "system" && Boolean(snapshot?.gpus.length) && <div className="sm-gpu-picker">
          <Cpu className="sm-gpu-picker-icon" size={15} aria-hidden="true" />
          <WorkbenchSelect key={snapshot!.gpus.map(gpu => gpu.uuid).join(",")} id={gpuSelectId} ariaLabel="选择趋势显卡" value={gpuSelection} onChange={setSelectedGpu}
            constrainToScrollParent options={[
              { value: "all", label: "全部 GPU", description: `${snapshot!.gpus.length} 张显卡` },
              ...snapshot!.gpus.map(gpu => ({ value: gpu.uuid, label: `GPU ${gpu.index}`, description: `${gpu.name} · ${bytes(gpu.memory_total_bytes)}` }))
            ]} />
        </div>}
        <div className="sm-trend-tabs" data-active={trend} role="group" aria-label="趋势指标">{([['system', 'CPU / 内存'], ['gpu', 'GPU 利用率'], ['gpuMemory', 'GPU 显存']] as const).map(([value, label]) => <button key={value} aria-pressed={trend === value} onClick={() => setTrend(value)}>{label}</button>)}</div></div></div>
        {(control.historyError || historyStale) && <div className="sm-history-notice" role="status">{history ? "历史数据暂未更新，当前展示最近一次读取的记录。" : "历史数据暂时不可用，正在等待采集服务恢复。"}</div>}
        <MonitoringTrendChart history={history} series={series} />
      </section>
      </div>
    </div></div>}
  </section>;
}
