#!/usr/bin/env node
/** Real-login acceptance on the development entries. Never records credentials or traces. */
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { parseArgs } from "node:util";
import { execFileSync } from "node:child_process";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const { values } = parseArgs({ options: {
  "password-file": { type: "string" },
  "output-dir": { type: "string", default: ".runtime/server-monitoring-9003/artifacts/browser" },
  "base-url": { type: "string", default: "http://127.0.0.1:9003" },
  "exercise-collector": { type: "boolean", default: false },
} });
assert(values["password-file"], "Supply a local password file; passwords must not be CLI arguments.");
const base = new URL(values["base-url"]);
assert(base.protocol === "http:" && ["localhost", "127.0.0.1"].includes(base.hostname) && ["9001", "9003", "29103"].includes(base.port));
const password = (await fs.readFile(values["password-file"], "utf8")).replace(/^\uFEFF/, "").trim();
assert(password.length, "Password file is empty.");
const output = path.resolve(values["output-dir"]);
await fs.mkdir(output, { recursive: true, mode: 0o700 });
await fs.chmod(output, 0o700);
const report = { started_at: new Date().toISOString(), accounts: [], blocked_mutations: [], unexpected_usage_activity_requests: [], errors: [] };
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });

async function login(page, username) {
  await page.getByTestId("server-monitoring-page").getByRole("button", { name: "登录账号", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "登录工作空间" });
  await dialog.getByLabel("用户名", { exact: true }).fill(username);
  await dialog.getByLabel("密码", { exact: true }).fill(password);
  await dialog.getByRole("button", { name: "登录", exact: true }).click();
  await page.locator(".sm-status").filter({ hasText: "在线" }).waitFor({ timeout: 30_000 });
  assert.equal(await page.getByTestId("server-monitoring-page").locator(".np-module-page-title").innerText(), "服务器节点");
  const modules = page.getByRole("navigation", { name: "业务模块", exact: true });
  const entries = await modules.getByRole("button").allTextContents();
  assert.equal(entries.indexOf("服务器节点"), entries.indexOf("智能助手") + 1, "server nodes follow the assistant in the sidebar");
  assert.equal(await modules.getByRole("button", { name: "服务器节点", exact: true }).getAttribute("aria-current"), "page");
  assert.equal(await page.locator(".sm-gpu").count(), 4);
  assert.match(await page.locator(".sm-host-meta").innerText(), /128 逻辑 CPU/);
}

async function logout(page, username) {
  await page.getByRole("button", { name: `账号菜单：${username}`, exact: true }).click();
  await page.getByRole("menuitem", { name: "退出登录", exact: true }).click();
  await page.getByTestId("server-monitoring-page").getByRole("button", { name: "登录账号", exact: true }).waitFor();
  assert.equal(await page.locator(".sm-host").count(), 0);
  assert.equal(await page.locator(".sm-gpu").count(), 0);
  assert.equal(await page.getByRole("complementary", { name: "主机信息", exact: true }).count(), 0);
  assert.equal(await page.locator(".gth-task").count(), 0, "logout removes retained GPU task observations");
  assert.equal(await page.locator(".gth-active-item").count(), 0, "logout removes current task observations");
}

async function assertSharedTheme(page) {
  const theme = await page.getByTestId("server-monitoring-page").evaluate(element => {
    // Resolve the shared tokens in this view; never navigate to another module.
    const probe = document.createElement("div");
    probe.setAttribute("aria-hidden", "true");
    probe.style.cssText = "position:fixed;visibility:hidden;pointer-events:none;background-color:var(--np-theme-bg);background-image:var(--np-theme-workspace-background);background-size:var(--np-theme-workspace-background-size);font-size:var(--np-theme-page-heading-size);line-height:var(--np-theme-page-heading-line-height);font-family:var(--font-heading);color:var(--np-theme-ink)";
    element.append(probe);
    try {
      const expected = getComputedStyle(probe), surface = getComputedStyle(element);
      const title = getComputedStyle(element.querySelector(".np-module-page-title"));
      const header = getComputedStyle(element.querySelector(".np-module-page-header"));
      return {
        actual: { background: surface.backgroundColor, image: surface.backgroundImage, size: surface.backgroundSize,
          titleSize: title.fontSize, titleHeight: title.lineHeight, titleFont: title.fontFamily, titleColor: title.color },
        expected: { background: expected.backgroundColor, image: expected.backgroundImage, size: expected.backgroundSize,
          titleSize: expected.fontSize, titleHeight: expected.lineHeight, titleFont: expected.fontFamily, titleColor: expected.color },
        headerBackground: header.backgroundColor, headerImage: header.backgroundImage,
        bodyFont: surface.fontFamily,
        typeRoles: [
          [".sm-section-heading h2", "14px"], [".sm-metric-label", "12px"], [".sm-host-meta, .sm-metric-note", "11px"],
        ].map(([selector, size]) => ({ size, actual: [...element.querySelectorAll(selector)].map(node => {
          const style = getComputedStyle(node); return { size: style.fontSize, font: style.fontFamily };
        }) })),
      };
    } finally { probe.remove(); }
  });
  assert.notEqual(theme.expected.image, "none", "shared workspace background tokens must resolve");
  assert.deepEqual(theme.actual, theme.expected, "monitoring background and heading must use the shared overview/tool tokens");
  assert.equal(theme.headerBackground, "rgba(0, 0, 0, 0)");
  assert.equal(theme.headerImage, "none", "module header preserves the shared workspace surface");
  for (const role of theme.typeRoles) {
    assert(role.actual.length > 0);
    for (const actual of role.actual) assert.deepEqual(actual, { size: role.size, font: theme.bodyFont },
      "section headings, body labels and captions use the shared 14/12/11 type scale and font");
  }
}

async function assertBottomReachable(page, label) {
  const scroll = page.locator(".sm-scroll");
  const overflows = await scroll.evaluate(element => element.scrollHeight > element.clientHeight + 2);
  if (overflows) {
    const bounds = await scroll.boundingBox();
    assert(bounds);
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + Math.min(bounds.height / 2, 400));
    await page.mouse.wheel(0, 10000);
    await page.waitForTimeout(350);
  }
  const bottom = await scroll.evaluate(element => {
    const viewport = element.getBoundingClientRect(), last = element.querySelector(".sm-history-layout").getBoundingClientRect();
    return { reached: element.scrollTop + element.clientHeight >= element.scrollHeight - 2,
      visible: last.bottom <= Math.min(viewport.bottom, innerHeight) + 2 && last.bottom > Math.max(viewport.top, 0) };
  });
  assert(bottom.reached && bottom.visible, `${label}: final panels must be reachable by scrolling or already visible when content fits`);
}

try {
  for (const username of ["zhiju000", "zhiju001", "zhiju002"]) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, timezoneId: "Asia/Shanghai", locale: "zh-CN" });
    let abortNextGpuTasks = false, abortedGpuTaskRequests = 0;
    // Allow the two explicit identity mutations only. No compute submission may escape this acceptance.
    await context.route("**/api/**", async route => {
      const request = route.request(), endpoint = new URL(request.url()).pathname;
      if (!["GET", "HEAD", "OPTIONS"].includes(request.method()) && !["/api/v1/auth/login", "/api/v1/auth/logout"].includes(endpoint)) {
        report.blocked_mutations.push({ method: request.method(), path: endpoint });
        return route.abort();
      }
      if (abortNextGpuTasks && request.method() === "GET" && endpoint === "/api/v1/server-monitoring/gpu-tasks") {
        abortNextGpuTasks = false;
        abortedGpuTaskRequests += 1;
        return route.abort("failed");
      }
      return route.continue();
    });
    let gpuTaskRequests = 0;
    context.on("request", request => {
      const endpoint = new URL(request.url()).pathname;
      if (endpoint === "/api/v1/server-monitoring/gpu-tasks" && request.method() === "GET") gpuTaskRequests += 1;
      if (endpoint === "/api/v1/usage/activity") {
        report.unexpected_usage_activity_requests.push({ username, method: request.method(), path: endpoint });
      }
    });
    const page = await context.newPage();
    const pageErrors = [];
    const observedSnapshots = [];
    const observedGpuTasks = [];
    let lastReadyTaskHistory = null, lastReadyGpuActivity = null;
    let gpuTasksReadFailed = false;
    page.on("response", async response => {
      const endpoint = new URL(response.url()).pathname;
      if (!["/api/v1/server-monitoring/snapshot", "/api/v1/server-monitoring/gpu-tasks"].includes(endpoint)) return;
      if (response.status() !== 200) {
        if (endpoint.endsWith("/gpu-tasks")) gpuTasksReadFailed = true;
        return;
      }
      try {
        const value = await response.json();
        if (endpoint.endsWith("/gpu-tasks")) {
          const observed = { data: value, receivedAt: Date.now() };
          const previous = observedGpuTasks.at(-1)?.data;
          if (previous && (previous.node_id !== value.node_id
            || [...previous.gpu_uuids].sort().join() !== [...value.gpu_uuids].sort().join())) {
            lastReadyTaskHistory = null;
            lastReadyGpuActivity = null;
          }
          if (lastReadyTaskHistory && Date.parse(lastReadyTaskHistory.data.history_captured_at) > Date.parse(value.captured_at)) lastReadyTaskHistory = null;
          if (lastReadyGpuActivity && Date.parse(lastReadyGpuActivity.data.activity.captured_at) > Date.parse(value.captured_at)) lastReadyGpuActivity = null;
          observedGpuTasks.push(observed);
          if (observedGpuTasks.length > 12) observedGpuTasks.shift();
          if (value.status === "ready" && new Set(value.sources.map(source => source.source_id)).size === 6
            && value.sources.every(source => source.status === "ready")) lastReadyTaskHistory = observed;
          if (value.activity.status === "ready") lastReadyGpuActivity = observed;
          gpuTasksReadFailed = false;
        } else {
          observedSnapshots.push(value);
          if (observedSnapshots.length > 3) observedSnapshots.shift();
        }
      } catch {
        if (endpoint.endsWith("/gpu-tasks")) gpuTasksReadFailed = true;
      }
    });
    page.on("requestfailed", request => {
      if (new URL(request.url()).pathname === "/api/v1/server-monitoring/gpu-tasks") gpuTasksReadFailed = true;
    });
    page.on("pageerror", error => pageErrors.push(error.name));
    await page.goto(new URL("/server-monitoring", base).href);
    await page.getByTestId("server-monitoring-page").getByRole("button", { name: "登录账号", exact: true }).waitFor();
    assert.equal(await page.locator(".sm-host").count(), 0, "guest cannot see cached metrics");
    const guest = await context.request.get(new URL("/api/v1/server-monitoring/snapshot", base).href);
    assert.equal(guest.status(), 401);
    const guestTasks = await context.request.get(new URL("/api/v1/server-monitoring/gpu-tasks", base).href);
    assert.equal(guestTasks.status(), 401);
    assert.equal(await page.locator(".gth-task").count(), 0, "guest cannot see retained GPU task observations");
    assert.equal(await page.locator(".gth-active-item").count(), 0, "guest cannot see current task observations");
    await login(page, username);
    const session = await (await context.request.get(new URL("/api/v1/auth/session", base).href)).json();
    assert.equal(session.user.username, username);
    const headers = { "X-Session-Context": session.session_id };
    const history = await (await context.request.get(new URL("/api/v1/server-monitoring/history", base).href, { headers })).json();
    const wrong = await context.request.get(new URL("/api/v1/server-monitoring/snapshot", base).href,
      { headers: { "X-Session-Context": "00000000-0000-0000-0000-000000000000" } });
    assert.equal(wrong.status(), 409);
    const wrongTasks = await context.request.get(new URL("/api/v1/server-monitoring/gpu-tasks", base).href,
      { headers: { "X-Session-Context": "00000000-0000-0000-0000-000000000000" } });
    assert.equal(wrongTasks.status(), 409);
    observedGpuTasks.length = 0;
    lastReadyTaskHistory = null; lastReadyGpuActivity = null;
    await page.reload();
    await page.locator(".sm-status").filter({ hasText: "在线" }).waitFor();
    await page.locator(".sm-chart").waitFor();
    await assertSharedTheme(page);
    const after = await (await context.request.get(new URL("/api/v1/server-monitoring/history", base).href, { headers })).json();
    assert.equal(after.collector_id, history.collector_id);
    assert.equal(after.started_at, history.started_at);
    assert(after.points.length >= history.points.length, "refresh preserves collected history");
    const inventory = await (await context.request.get(new URL("/api/v1/server-monitoring/snapshot", base).href, { headers })).json();
    assert.equal(inventory.collector_id, after.collector_id, "snapshot and history identify the same collector run");
    assert.equal(inventory.started_at, after.started_at, "snapshot and history share the actual collector start time");
    assert(after.points.length > 0, "a new collector only needs an actual completed sampling window, not a full hour");
    assert(after.points.every(point => Date.parse(point.timestamp) >= Date.parse(after.started_at)
      && Date.parse(point.timestamp) <= Date.parse(after.captured_at)), "history belongs to the current collector lifetime");
    const tasksResponse = await context.request.get(new URL("/api/v1/server-monitoring/gpu-tasks", base).href, { headers });
    assert.equal(tasksResponse.status(), 200);
    assert.match(tasksResponse.headers()["cache-control"] ?? "", /private.*no-store/);
    const gpuTasks = await tasksResponse.json();
    const gpuTasksReceivedAt = Date.now();
    assert.equal(gpuTasks.node_id, inventory.node_id, "GPU task history belongs to the current physical server");
    assert.equal(gpuTasks.coverage, "retained_verified_gpu_records");
    assert(["ready", "partial", "unavailable"].includes(gpuTasks.status));
    assert(["fresh", "stale", "unavailable"].includes(gpuTasks.freshness));
    assert(["ready", "partial", "unavailable"].includes(gpuTasks.activity.status));
    const activityAge = gpuTasks.age_seconds + (Date.parse(gpuTasks.captured_at) - Date.parse(gpuTasks.activity.captured_at)) / 1000;
    assert(Number.isFinite(activityAge) && activityAge >= 0, "cached activity keeps its actual observation age");
    assert(Date.parse(gpuTasks.history_captured_at) <= Date.parse(gpuTasks.captured_at));
    assert.deepEqual([...gpuTasks.gpu_uuids].sort(), inventory.gpus.map(gpu => gpu.uuid).sort());
    const tasksForGpu = (index, data = gpuTasks) => {
      const device = index === null ? null : inventory.gpus.find(gpu => gpu.index === index);
      assert(index === null || device, "physical GPU must exist in the real inventory");
      return data.tasks.filter(task => index === null || task.gpu_uuid === device.uuid)
        .sort((left, right) => Date.parse(right.finished_at) - Date.parse(left.finished_at) || left.id.localeCompare(right.id));
    };
    const runningForGpu = (index, data = gpuTasks) => {
      const device = index === null ? null : inventory.gpus.find(gpu => gpu.index === index);
      assert(index === null || device);
      return data.activity.items.filter(item => (index === null || item.gpu_uuid === device.uuid) && item.kind === "task" && item.status === "running");
    };
    const currentTaskEvidence = () => {
      const latest = observedGpuTasks.at(-1) ?? { data: gpuTasks, receivedAt: gpuTasksReceivedAt };
      const ageOf = timestamp => latest.data.age_seconds + Math.max(0, Date.now() - latest.receivedAt) / 1000
        + Math.max(0, Date.parse(latest.data.captured_at) - Date.parse(timestamp)) / 1000;
      const historyReady = latest.data.status === "ready" && new Set(latest.data.sources.map(source => source.source_id)).size === 6
        && latest.data.sources.every(source => source.status === "ready");
      const activityReady = latest.data.activity.status === "ready";
      return {
        latest: latest.data,
        history: (historyReady ? latest : lastReadyTaskHistory ?? latest).data,
        activity: (activityReady ? latest : lastReadyGpuActivity ?? latest).data,
        historyConfirmed: historyReady && ageOf(latest.data.history_captured_at) <= 90 && !gpuTasksReadFailed,
        activityConfirmed: activityReady && latest.data.freshness === "fresh"
          && ageOf(latest.data.activity.captured_at) <= 15 && !gpuTasksReadFailed,
      };
    };
    const indexForTask = task => {
      const device = inventory.gpus.find(gpu => gpu.uuid === task.gpu_uuid);
      assert(device, "task navigation requires a verified current GPU UUID");
      return device.index;
    };
    for (const [index, environment] of [[1, "development"], [2, "production"]]) {
      const retained = tasksForGpu(index);
      assert(retained.length <= 10, "verified history retains at most ten records per GPU across all modules");
      if (gpuTasks.status === "ready") assert.equal(retained.length, 10, "a complete scan includes the known retained records");
      assert(retained.every(task => task.environment === environment && ["monomer_dft", "monomer_md", "polymer_md"].includes(task.module) && task.status === "completed"));
      for (const task of retained) {
        const suffix = { monomer_dft: "dft", monomer_md: "md", polymer_md: "polymer_md" }[task.module];
        const source = gpuTasks.sources.find(item => item.source_id === `${environment}_${suffix}`);
        assert(source && ["ready", "partial"].includes(source.status) && source.matched_tasks > 0,
          "each task row requires a verified module source, including retained records during a partial read");
        assert(Math.abs((Date.parse(task.finished_at) - Date.parse(task.started_at)) / 1000 - task.duration_seconds) <= 0.001,
          "retained completion duration still matches its real execution timestamps");
        assert(Date.parse(task.finished_at) <= Date.parse(gpuTasks.history_captured_at), "cached history cannot claim a completion after its source capture");
      }
    }
    const developmentMd = gpuTasks.sources.find(source => source.source_id === "development_md");
    const productionMd = gpuTasks.sources.find(source => source.source_id === "production_md");
    assert(developmentMd && productionMd, "the response identifies both fixed MD sources even during partial reads");
    if (developmentMd.status === "ready") assert(developmentMd.matched_tasks >= 17, "a complete scan includes verified MD records alongside DFT");
    if (productionMd.status === "ready") assert(productionMd.excluded_tasks >= 15, "old MD records without physical GPU evidence remain excluded");
    for (const sourceId of ["development_polymer_md", "production_polymer_md"]) {
      const source = gpuTasks.sources.find(item => item.source_id === sourceId);
      assert(source && ["ready", "partial", "unavailable"].includes(source.status));
      if (source.status === "ready") assert(source.excluded_tasks >= 2, "polymer GPU-count settings are not physical GPU evidence");
    }
    for (const index of [0, 3]) assert.equal(tasksForGpu(index).length, 0, "unassociated GPUs must not inherit another device's history");
    await page.getByRole("button", { name: "CPU / 内存", exact: true }).click();
    const cards = page.locator(".sm-inspection-card");
    assert.equal(await cards.count(), 5);
    for (const card of await cards.all()) assert.equal(await card.getByRole("progressbar").count(), 4);
    assert.equal(await page.locator(".sm-server-grid .sm-inspection-card").count(), 1, "top switcher contains only the current server");
    assert.equal(await page.locator(".sm-detail-panel .sm-metric").count(), 6, "server details contain six metrics");
    assert.equal(await page.getByRole("heading", { name: "服务器详情", exact: true }).count(), 1);
    const hostInfo = page.getByRole("complementary", { name: "主机信息", exact: true });
    const hostInfoRow = label => hostInfo.locator(".sm-host-info-list > div").filter({ has: page.getByText(label, { exact: true }) });
    const hostMetadata = inventory.host_info;
    assert(hostMetadata && typeof hostMetadata === "object", "real acceptance requires the deployed host metadata collector");
    for (const field of ["cpu_model", "os_name", "kernel_release", "nvidia_driver_version"]) {
      assert(typeof hostMetadata[field] === "string" && hostMetadata[field].trim().length > 0,
        `the current host must provide its actual ${field}`);
    }
    for (const field of ["cpu_socket_count", "cpu_physical_core_count"]) {
      assert(Number.isInteger(hostMetadata[field]) && hostMetadata[field] > 0,
        `the current host must provide its actual ${field}`);
    }
    const formatAvailable = value => {
      if (typeof value !== "number" || !Number.isFinite(value)) return "--";
      const unit = value >= 2 ** 40 ? "TiB" : value >= 2 ** 30 ? "GiB" : "MiB";
      return `${(value / (unit === "TiB" ? 2 ** 40 : unit === "GiB" ? 2 ** 30 : 2 ** 20)).toFixed(1)} ${unit}`;
    };
    const assertHostInfo = async label => {
      assert.equal(await hostInfo.getByRole("heading", { name: "主机信息", exact: true }).count(), 1);
      assert.equal(await page.getByRole("complementary", { name: "资源余量", exact: true }).count(), 0);
      for (const [name, value, note] of [
        ["CPU", hostMetadata.cpu_model ?? "--", `${hostMetadata.cpu_socket_count ?? "--"} 插槽 · ${hostMetadata.cpu_physical_core_count ?? "--"} 核 · ${inventory.logical_cpu_count} 线程`],
        ["系统", hostMetadata.os_name ?? "--", hostMetadata.kernel_release ?? "--"],
        ["GPU 驱动", hostMetadata.nvidia_driver_version ?? "--"],
      ]) {
        const row = hostInfoRow(name);
        assert.equal((await row.locator(".sm-host-info-value").innerText()).trim(), value,
          `${label}: ${name} comes from the authenticated host metadata`);
        if (note !== undefined) assert.equal((await row.locator(".sm-host-info-note").innerText()).trim(), note);
      }
      // Compare changing capacities with actual responses delivered to the page,
      // including the previous in-flight render; never derive available from used.
      const recent = observedSnapshots.filter(snapshot => snapshot.collector_id === inventory.collector_id);
      assert(recent.length > 0, "host information must have an actual browser snapshot response");
      for (const [diskLabel, mount] of [["系统盘", "/"], ["数据盘", "/data"]]) {
        const disk = hostInfoRow("磁盘可用").locator(".sm-host-disk").filter({ has: page.getByText(diskLabel, { exact: true }) });
        const displayed = (await disk.locator("strong").innerText()).trim();
        assert(recent.some(snapshot => displayed === formatAvailable(snapshot.disks.find(item => item.mount_point === mount)?.available_bytes)),
          `${label}: ${diskLabel} available capacity matches a recent displayed snapshot`);
      }
      const geometry = await hostInfo.evaluate(element => {
        const panel = element.getBoundingClientRect();
        return { height: panel.height, fits: panel.left >= -1 && panel.right <= innerWidth + 1
          && element.scrollWidth <= element.clientWidth + 1
          && [...element.querySelectorAll(".sm-host-info-list > div, dd, .sm-host-disk")].every(row => {
            const bounds = row.getBoundingClientRect();
            return bounds.left >= panel.left - 1 && bounds.right <= panel.right + 1 && row.scrollWidth <= row.clientWidth + 1;
          }) };
      });
      assert(geometry.fits, `${label}: host information fits without horizontal overflow`);
      assert(geometry.height > 0 && geometry.height <= 320, `${label}: host information remains a compact summary`);
    };
    await assertHostInfo("desktop");
    assert.equal(await page.getByRole("heading", { name: "我的近期任务", exact: true }).count(), 0);
    assert.equal(await page.getByText("已采集", { exact: true }).count(), 0);
    assert.equal(await page.getByText("采样正常", { exact: true }).count(), 0);
    assert.equal(await page.locator(".sm-sample-time, .sm-resource-summary-note, .sm-trend-footer, .sm-footer, .sm-last-success").count(), 0,
      "normal monitoring view omits collection timestamps and technical footers");
    const monitoringText = await page.getByTestId("server-monitoring-page").innerText();
    for (const explanation of ["快照 5 秒", "趋势 30 秒", "每 30 秒记录一个观测", "采样缺失处保留空缺", "切换显卡保留当前趋势指标", "本次采集起点", "纵轴固定", "切换到后台时暂停刷新"]) {
      assert(!monitoringText.includes(explanation), "normal monitoring view omits implementation explanations");
    }
    assert(await page.locator(".sm-detail-panel .sm-metric-note").evaluateAll(elements => elements.every(element => getComputedStyle(element).whiteSpace === "nowrap")), "parameter notes use one line");
    assert.equal(await page.locator(".sm-storage, .sm-collection").count(), 0, "separate bottom sections have been removed");
    const gpuRows = await page.locator(".sm-gpu-grid .sm-inspection-card").evaluateAll(elements => elements.map(element => Math.round(element.getBoundingClientRect().top)));
    assert.equal(gpuRows.length, 4);
    assert.equal(new Set(gpuRows).size, 1, "all four GPUs share one desktop row");
    const metricRows = await page.locator(".sm-detail-panel .sm-metric").evaluateAll(elements => elements.map(element => Math.round(element.getBoundingClientRect().top)));
    assert.deepEqual([...new Set(metricRows)].map(top => metricRows.filter(item => item === top).length), [3, 3], "server metrics form three columns by two rows");
    const trendLabels = ["CPU / 内存", "GPU 显存", "GPU 利用率"];
    const trendControls = page.getByRole("group", { name: "趋势指标", exact: true });
    const gpuSelect = page.getByRole("combobox", { name: "选择趋势显卡", exact: true });
    const gpuMenu = page.getByRole("listbox", { name: "选择趋势显卡", exact: true });
    const gpuOption = label => gpuMenu.getByRole("option").filter({ has: page.getByText(label, { exact: true }) });
    const legend = page.locator(".sm-chart-legend > span");
    const taskPanel = page.getByRole("complementary", { name: "GPU 任务", exact: true });
    const historicalLabels = { single_point: "DFT 单点能", optimization: "DFT 结构优化", md_formal: "单体 MD · 正式", md_demo: "单体 MD · 演示", polymer_md: "聚合物 MD" };
    assert.equal(await page.getByRole("combobox", { name: "筛选完成任务类型", exact: true }).count(), 0);
    const assertGpuTasks = async (index, { desktopLayout = true } = {}) => {
      const evidence = currentTaskEvidence();
      assert.equal((await taskPanel.locator(".sm-gpu-tasks-scope").innerText()).trim(), index === null ? "全部 GPU" : `GPU ${index}`);
      assert.equal(await taskPanel.getByRole("button", { name: "查看全部 GPU 任务", exact: true }).count(), index === null ? 0 : 1);
      assert.equal(await taskPanel.locator(".sm-gpu-tasks-placeholder").count(), 0, "all-GPU mode displays aggregated tasks instead of an empty selection prompt");
      assert.equal(await taskPanel.getByRole("heading", { name: /^当前正在进行/ }).count(), 1);
      assert.equal(await taskPanel.getByRole("heading", { name: /^近期完成任务/ }).count(), 1);
      const expected = tasksForGpu(index, evidence.history).slice(0, 2);
      const historyRegion = taskPanel.getByRole("region", { name: "近期完成任务", exact: true });
      const rows = taskPanel.locator(".gth-list > .gth-task");
      if (expected.length === 0) {
        await historyRegion.getByText(evidence.historyConfirmed ? "暂无可关联的历史记录" : "--", { exact: true }).waitFor();
        assert.equal(await rows.count(), 0);
      } else {
        await rows.first().waitFor();
        assert.equal(await rows.count(), expected.length);
        assert.equal(await taskPanel.getByText("暂无可关联的历史记录", { exact: true }).count(), 0);
        for (const [position, task] of expected.entries()) {
          const row = rows.nth(position);
          assert.equal(await row.locator(".gth-task-heading strong").innerText(), historicalLabels[task.calculation_type]);
          assert.equal(await row.locator(".gth-environment").innerText(), task.environment === "development" ? "开发" : "生产");
          assert.equal(await row.locator(".gth-completed").innerText(), "已完成");
          assert.equal(await row.locator("time").getAttribute("datetime"), task.finished_at, "task completion time must match the authenticated monitoring API");
          assert.equal(await row.locator(".gth-task-meta > span").getAttribute("title"), `${task.duration_seconds} 秒`);
          if (index === null) {
            assert.equal(await row.locator(".gth-gpu-link").count(), 1);
            assert.equal((await row.locator(".gth-gpu-link").innerText()).trim(), `GPU ${indexForTask(task)}`);
            assert.equal(await row.getByRole("button", { name: `切换到 GPU ${indexForTask(task)}`, exact: true }).count(), 1,
              "each aggregate completion links to its actual physical GPU");
          }
        }
      }
      const runningRegion = taskPanel.getByRole("region", { name: "当前正在进行", exact: true });
      const liveRows = runningRegion.locator(".gth-active-list > .gth-active-item");
      const expectedRunning = runningForGpu(index, evidence.activity);
      if (expectedRunning.length === 0) {
        await runningRegion.getByText(evidence.activityConfirmed ? "暂无正在进行的任务" : "--", { exact: true }).waitFor({ timeout: 15_000 });
        assert.equal(await liveRows.count(), 0, "resident services and unclassified GPU processes are not running tasks");
      } else {
        await liveRows.first().waitFor({ timeout: 15_000 });
        assert.equal(await liveRows.count(), expectedRunning.length, "only verified running tasks from the selected GPU are shown");
        assert.deepEqual((await liveRows.locator(".gth-task-heading strong").allTextContents()).sort(), expectedRunning.map(item => item.label).sort());
        for (const row of await liveRows.all()) assert.equal((await row.locator(".gth-kind").innerText()).trim(),
          evidence.activityConfirmed ? "任务运行中" : "最近运行任务", "cached activity must not claim to be currently running");
        if (index === null) assert.deepEqual((await liveRows.locator(".gth-gpu-link").allTextContents()).map(label => label.trim()).sort(),
          expectedRunning.map(item => `GPU ${indexForTask(item)}`).sort(), "each aggregate running task links to its verified GPU");
        assert.equal(await runningRegion.getByText("暂无正在进行的任务", { exact: true }).count(), 0);
      }
      assert.equal(await taskPanel.locator(".gth-gpu-link").count(), index === null ? expected.length + expectedRunning.length : 0,
        "GPU task navigation is shown only in the aggregate view");
      assert.equal(await runningRegion.locator(".gth-kind.is-service, .gth-kind.is-process").count(), 0);
      assert.doesNotMatch(await runningRegion.innerText(), /常驻服务|GPU\s*进程|DFT服务|平台服务|平台后端|NVIDIA MPS服务/,
        "service residency and unidentified processes must never be presented as current tasks");
      assert.equal(await taskPanel.locator(".gth-notice, .gth-retry").count(), 0, "ordinary source or transport failures keep the task card quiet");
      assert.equal(await taskPanel.locator(".gth-access-notice, .gth-access-retry").count(), 0, "real authenticated reads must not show an authorization recovery prompt");
      assert.equal(await taskPanel.locator(".gth-updated, .gth-scope, .gth-group-heading > time").count(), 0,
        "task panels omit observation timestamps and technical coverage explanations");
      assert.doesNotMatch(await taskPanel.innerText(), /最近观测|可核验 GPU 归属|活动采样|历史扫描/);
      if (desktopLayout) {
        const layout = await page.locator(".sm-history-layout").evaluate(element => {
          const left = element.querySelector(".sm-gpu-tasks").getBoundingClientRect();
          const right = element.querySelector(".sm-trends").getBoundingClientRect();
          return { leftX: left.x, leftRight: left.right, rightX: right.x, taskWidth: left.width, trendWidth: right.width,
            topDifference: Math.abs(left.top - right.top), heightDifference: Math.abs(left.height - right.height) };
        });
        assert(layout.leftX < layout.rightX && layout.leftRight <= layout.rightX && layout.topDifference <= 2 && layout.heightDifference <= 2,
          "desktop GPU tasks and resource trend must sit side by side with equal heights");
        assert(layout.taskWidth >= 280 && layout.taskWidth / layout.trendWidth < 0.45,
          "desktop task panel stays readable while giving the trend more width");
      }
    };
    const assertTrend = async (selected, labels) => {
      for (const label of trendLabels) {
        assert.equal(await trendControls.getByRole("button", { name: label, exact: true }).getAttribute("aria-pressed"), String(label === selected), "GPU selection must preserve the selected trend metric");
      }
      assert.deepEqual((await legend.allTextContents()).map(value => value.trim()), labels);
      assert.equal(await gpuSelect.count(), selected === "CPU / 内存" ? 0 : 1);
    };
    const assertSelectedCard = async index => {
      for (const otherIndex of [0, 1, 2, 3]) {
        assert.equal(await page.getByRole("button", { name: `查看GPU ${otherIndex}详情`, exact: true }).getAttribute("aria-pressed"), String(otherIndex === index), "card selection is independent of the trend metric");
      }
    };
    const assertSelectedGpu = async index => {
      if (index !== null) assert(inventory.gpus.some(gpu => gpu.index === index), "selection must refer to an actual physical GPU");
      assert.equal((await gpuSelect.locator("strong").innerText()).trim(), index === null ? "全部 GPU" : `GPU ${index}`,
        "trend selector must identify the clicked GPU");
    };
    const assertGpuMenu = async selectedIndex => {
      await gpuMenu.waitFor();
      await page.waitForTimeout(200);
      assert.equal(await gpuSelect.getAttribute("aria-expanded"), "true");
      assert.deepEqual(await gpuMenu.getByRole("option").locator("strong").allTextContents(), ["全部 GPU", ...inventory.gpus.map(gpu => `GPU ${gpu.index}`)]);
      assert.equal(await gpuOption("全部 GPU").locator("small").innerText(), `${inventory.gpus.length} 张显卡`);
      for (const gpu of inventory.gpus) {
        const option = gpuOption(`GPU ${gpu.index}`), description = await option.locator("small").innerText();
        assert(description.startsWith(`${gpu.name} · `), "GPU options display the actual device model");
        const capacity = description.match(/([\d,.]+)\s*GiB/);
        assert(capacity && Math.abs(Number(capacity[1].replaceAll(",", "")) - gpu.memory_total_bytes / 2 ** 30) <= 0.1,
          "GPU option capacity comes from the physical inventory");
        assert.equal(await option.getAttribute("aria-selected"), String(gpu.index === selectedIndex));
      }
      assert.equal(await gpuOption("全部 GPU").getAttribute("aria-selected"), String(selectedIndex === null));
      const bounds = await gpuMenu.evaluate(element => {
        const menu = element.getBoundingClientRect(), scroll = element.closest(".sm-scroll").getBoundingClientRect();
        return { visible: menu.height > 20 && menu.top >= Math.max(0, scroll.top) - 2
          && menu.bottom <= Math.min(innerHeight, scroll.bottom) + 2 && menu.left >= 0 && menu.right <= innerWidth + 1,
          noHorizontalOverflow: element.scrollWidth <= element.clientWidth + 1,
          aboveChartAndTabs: [...element.querySelectorAll('[role="option"]')].every(option => {
            const rect = option.getBoundingClientRect(), top = Math.max(rect.top, menu.top), bottom = Math.min(rect.bottom, menu.bottom);
            if (bottom - top < 8) return true;
            return option.contains(document.elementFromPoint(rect.left + rect.width / 2, (top + bottom) / 2));
          }) };
      });
      assert(bounds.visible && bounds.noHorizontalOverflow, "GPU menu must fit the viewport and its scrolling work surface");
      assert(bounds.aboveChartAndTabs, "GPU options must stay above the SVG chart and trend tabs");
    };
    const chooseGpu = async index => {
      await gpuSelect.click();
      await gpuOption(index === null ? "全部 GPU" : `GPU ${index}`).click();
      await gpuMenu.waitFor({ state: "hidden" });
      assert.equal(await gpuSelect.getAttribute("aria-expanded"), "false");
      await assertSelectedGpu(index);
    };
    const plot = page.getByRole("group", { name: "趋势采样读数", exact: true });
    const tooltip = page.getByRole("tooltip");
    const assertTooltipReading = async (metric, index) => {
      await tooltip.waitFor();
      const timestamp = await tooltip.locator("time").getAttribute("datetime");
      const rendered = await tooltip.locator("dl > div").evaluateAll(rows => rows.map(row => ({
        label: row.querySelector("dt").textContent.trim(), value: row.querySelector("dd").textContent.trim(),
      })));
      const response = await context.request.get(new URL("/api/v1/server-monitoring/history", base).href, { headers });
      assert.equal(response.status(), 200);
      const observedHistory = await response.json();
      const point = observedHistory.points.find(sample => sample.timestamp === timestamp);
      assert(point, "tooltip must identify an actual retained sample, not an interpolated timestamp");
      const reading = value => value === null || !Number.isFinite(value) || value < 0 || value > 100 ? "--"
        : `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)}%`;
      let expected;
      if (metric === "CPU / 内存") {
        expected = [{ label: "CPU", value: reading(point.cpu_percent) }, { label: "内存", value: reading(point.memory_percent) }];
      } else {
        const device = inventory.gpus.find(gpu => gpu.index === index);
        const sample = point.gpus.find(gpu => gpu.uuid === device.uuid);
        const value = metric === "GPU 利用率" ? sample?.utilization_percent ?? null
          : sample?.memory_used_bytes != null && device.memory_total_bytes > 0 ? sample.memory_used_bytes / device.memory_total_bytes * 100 : null;
        expected = [{ label: `GPU ${index}`, value: reading(value) }];
      }
      assert.deepEqual(rendered, expected, "tooltip values must match the selected metric and physical GPU at that sample");
      assert.equal(await page.locator(".sm-trend-crosshair").count(), 1);
      return timestamp;
    };
    await assertGpuTasks(null);
    for (const label of trendLabels) {
      await trendControls.getByRole("button", { name: label, exact: true }).click();
      if (label !== "CPU / 内存") {
        // The preceding system/memory check ends with GPU 3 selected. Changing
        // metric preserves that device, including a selection made in system mode.
        await assertSelectedCard(3);
        await assertSelectedGpu(3);
        await assertTrend(label, ["GPU 3"]);
      }
      for (const index of [0, 1, 2, 3]) {
        const card = page.getByRole("button", { name: `查看GPU ${index}详情`, exact: true });
        if (index === 0) { await card.focus(); await card.press("Enter"); }
        else await card.click();
        await assertSelectedCard(index);
        assert.equal(await page.locator(".sm-detail-panel .sm-metric").count(), 6, "GPU selection preserves host parameters");
        assert.match(await page.locator(".sm-host-meta").innerText(), /128 逻辑 CPU/);
        await assertTrend(label, label === "CPU / 内存" ? ["CPU", "内存"] : [`GPU ${index}`]);
        if (label !== "CPU / 内存") await assertSelectedGpu(index);
        await assertGpuTasks(index);
        if (username === "zhiju000" && label !== "CPU / 内存" && [0, 3].includes(index)) {
          await plot.focus(); await plot.press("End");
          await assertTooltipReading(label, index);
          await plot.press("Escape");
          await tooltip.waitFor({ state: "hidden" });
        }
      }
    }
    // The shared selector keeps mouse, keyboard and dismissal behavior intact.
    await gpuSelect.click();
    await assertGpuMenu(3);
    await gpuSelect.press("Escape");
    await gpuMenu.waitFor({ state: "hidden" });
    assert(await gpuSelect.evaluate(element => document.activeElement === element), "Escape preserves trigger focus");
    await assertSelectedGpu(3);
    await gpuSelect.click(); await gpuMenu.waitFor();
    await page.locator(".sm-trends .sm-section-heading h2").click();
    await gpuMenu.waitFor({ state: "hidden" });
    assert.equal(await gpuSelect.getAttribute("aria-expanded"), "false", "outside click closes the GPU menu");
    await assertSelectedGpu(3);
    await chooseGpu(null);
    await assertTrend("GPU 利用率", ["GPU 0", "GPU 1", "GPU 2", "GPU 3"]);
    await assertSelectedCard(null);
    await assertGpuTasks(null);
    const linkedGpu = indexForTask(tasksForGpu(null)[0]);
    await taskPanel.locator(".gth-list > .gth-task").first().getByRole("button", { name: `切换到 GPU ${linkedGpu}`, exact: true }).click();
    await assertSelectedCard(linkedGpu); await assertSelectedGpu(linkedGpu);
    await assertTrend("GPU 利用率", [`GPU ${linkedGpu}`]);
    await assertGpuTasks(linkedGpu);
    await taskPanel.getByRole("button", { name: "查看全部 GPU 任务", exact: true }).click();
    await assertSelectedCard(null); await assertSelectedGpu(null);
    await assertTrend("GPU 利用率", ["GPU 0", "GPU 1", "GPU 2", "GPU 3"]);
    await assertGpuTasks(null);
    await gpuSelect.focus(); await gpuSelect.press("ArrowDown");
    await gpuMenu.waitFor();
    await gpuSelect.press("Home"); await gpuSelect.press("ArrowDown"); await gpuSelect.press("Enter");
    await gpuMenu.waitFor({ state: "hidden" });
    await assertSelectedGpu(0);
    await assertSelectedCard(0);
    await assertTrend("GPU 利用率", ["GPU 0"]);
    await assertGpuTasks(0);
    await page.getByRole("button", { name: "查看当前服务器详情", exact: true }).click();
    await assertTrend("CPU / 内存", ["CPU", "内存"]);
    await assertSelectedCard(null);
    await assertGpuTasks(null);
    await trendControls.getByRole("button", { name: "GPU 显存", exact: true }).click();
    await assertSelectedGpu(null);
    await assertTrend("GPU 显存", ["GPU 0", "GPU 1", "GPU 2", "GPU 3"]);
    await assertGpuTasks(null);
    await page.getByRole("button", { name: "查看当前服务器详情", exact: true }).click();
    await assertTrend("CPU / 内存", ["CPU", "内存"]);
    await assertSelectedCard(null);
    await assertGpuTasks(null);
    if (username === "zhiju000") {
      const chart = page.locator(".sm-chart");
      const chartBounds = await chart.boundingBox();
      assert(chartBounds);
      await chart.hover({ position: { x: chartBounds.width / 2, y: chartBounds.height / 2 } });
      await assertTooltipReading("CPU / 内存");
      await page.mouse.move(1, 1);
      await tooltip.waitFor({ state: "hidden" });
      await plot.focus(); await plot.press("End");
      const latestTime = await assertTooltipReading("CPU / 内存");
      await plot.press("ArrowLeft");
      const previousTime = await assertTooltipReading("CPU / 内存");
      const currentHistory = await (await context.request.get(new URL("/api/v1/server-monitoring/history", base).href, { headers })).json();
      const latestPosition = currentHistory.points.findIndex(point => point.timestamp === latestTime);
      assert(latestPosition >= 0, "the selected sample is still in the current history");
      assert.equal(previousTime, currentHistory.points[Math.max(0, latestPosition - 1)].timestamp,
        "keyboard navigation selects the previous real sample, or keeps the only available sample");
      await plot.press("Escape");
      await tooltip.waitFor({ state: "hidden" });
      report.trend_readouts = { hover: "passed", keyboard: "passed", selected_gpu_values: "passed" };
      // System trends have no GPU dropdown: task scope still provides a return path.
      await taskPanel.locator(".gth-list > .gth-task").first().getByRole("button", { name: `切换到 GPU ${linkedGpu}`, exact: true }).click();
      await assertSelectedCard(linkedGpu); await assertTrend("CPU / 内存", ["CPU", "内存"]);
      await assertGpuTasks(linkedGpu);
      await taskPanel.getByRole("button", { name: "查看全部 GPU 任务", exact: true }).click();
      await assertSelectedCard(null); await assertTrend("CPU / 内存", ["CPU", "内存"]);
      await assertGpuTasks(null);
      await assertBottomReachable(page, "desktop aggregate tasks");
      await page.screenshot({ path: path.join(output, "monitoring-all-gpu-tasks-desktop.png"), fullPage: true });
      // Capture a populated, physically attributed history panel alongside the unchanged system trend.
      await page.getByRole("button", { name: "查看GPU 1详情", exact: true }).click();
      await assertSelectedCard(1);
      await assertTrend("CPU / 内存", ["CPU", "内存"]);
      await assertGpuTasks(1);
      await page.locator(".sm-scroll").evaluate(element => { element.scrollTop = 0; });
      await page.waitForTimeout(350);
      await page.screenshot({ path: path.join(output, "monitoring-desktop.png"), fullPage: true });
      await hostInfo.screenshot({ path: path.join(output, "monitoring-host-info-desktop.png") });
      await assertBottomReachable(page, "desktop");
      await page.screenshot({ path: path.join(output, "monitoring-desktop-bottom.png"), fullPage: true });
      await trendControls.getByRole("button", { name: "GPU 显存", exact: true }).click();
      await assertTrend("GPU 显存", ["GPU 1"]);
      await gpuSelect.click(); await assertGpuMenu(1);
      await page.screenshot({ path: path.join(output, "monitoring-gpu-select-desktop.png"), fullPage: true });
      await gpuSelect.press("Escape"); await gpuMenu.waitFor({ state: "hidden" });
      await trendControls.getByRole("button", { name: "CPU / 内存", exact: true }).click();
      await page.locator(".sm-scroll").evaluate(element => { element.scrollTop = 0; });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForTimeout(350);
      await assertSharedTheme(page);
      await assertHostInfo("mobile");
      await page.screenshot({ path: path.join(output, "monitoring-mobile.png"), fullPage: true });
      await hostInfo.screenshot({ path: path.join(output, "monitoring-host-info-mobile.png") });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), "mobile layout must fit viewport");
      assert(await page.locator(".sm-history-layout > *").evaluateAll(elements => elements.every(element => {
        const bounds = element.getBoundingClientRect();
        return bounds.left >= -1 && bounds.right <= innerWidth + 1 && element.scrollWidth <= element.clientWidth + 1;
      })), "mobile GPU task history and trend panels must fit without horizontal overflow");
      assert.equal(await taskPanel.locator(".gth-task").count(), Math.min(2, tasksForGpu(1, currentTaskEvidence().history).length),
        "mobile shows at most the latest two verified completions");
      await assertBottomReachable(page, "mobile");
      const mobileChart = await page.locator(".sm-chart").evaluate(element => ({
        plotHeight: element.querySelector(".sm-chart-grid-vertical").getBoundingClientRect().height,
        tickHeight: element.querySelector(".sm-chart-tick").getBoundingClientRect().height,
      }));
      assert(mobileChart.plotHeight >= 140 && mobileChart.tickHeight >= 9,
        "mobile trends keep readable labels and plot height instead of shrinking the desktop SVG");
      await page.screenshot({ path: path.join(output, "monitoring-mobile-bottom.png"), fullPage: true });
      await trendControls.getByRole("button", { name: "GPU 显存", exact: true }).click();
      await assertTrend("GPU 显存", ["GPU 1"]);
      await gpuSelect.click(); await assertGpuMenu(1);
      await page.screenshot({ path: path.join(output, "monitoring-gpu-select-mobile.png"), fullPage: true });
      await gpuOption("GPU 2").click(); await gpuMenu.waitFor({ state: "hidden" });
      await assertSelectedGpu(2); await assertSelectedCard(2);
      await assertTrend("GPU 显存", ["GPU 2"]);
      await assertGpuTasks(2, { desktopLayout: false });
      await chooseGpu(null); await assertSelectedCard(null);
      await assertTrend("GPU 显存", ["GPU 0", "GPU 1", "GPU 2", "GPU 3"]);
      await assertGpuTasks(null, { desktopLayout: false });
      await taskPanel.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(output, "monitoring-all-gpu-tasks-mobile.png"), fullPage: true });
      await taskPanel.locator(".gth-list > .gth-task").first().getByRole("button", { name: `切换到 GPU ${linkedGpu}`, exact: true }).click();
      await assertSelectedGpu(linkedGpu); await assertSelectedCard(linkedGpu);
      await assertTrend("GPU 显存", [`GPU ${linkedGpu}`]);
      await assertGpuTasks(linkedGpu, { desktopLayout: false });
      await taskPanel.getByRole("button", { name: "查看全部 GPU 任务", exact: true }).click();
      await assertSelectedGpu(null); await assertSelectedCard(null);
      await assertTrend("GPU 显存", ["GPU 0", "GPU 1", "GPU 2", "GPU 3"]);
      await assertGpuTasks(null, { desktopLayout: false });
      await chooseGpu(1); await assertSelectedCard(1);
      await trendControls.getByRole("button", { name: "CPU / 内存", exact: true }).click();
      report.gpu_picker = { keyboard: "passed", escape: "passed", outside_click: "passed", selected_state: "passed", desktop_bounds: "passed", mobile_bounds: "passed" };
      await page.setViewportSize({ width: 1440, height: 1100 });
      await page.locator(".sm-scroll").evaluate(element => { element.scrollTop = 0; });
      // Exercise one failed read after real authentication and real records have
      // been rendered. No API payload or login response is replaced.
      await assertGpuTasks(1);
      const completionProjection = () => taskPanel.locator(".gth-list > .gth-task").evaluateAll(rows => rows.map(row => ({
        label: row.querySelector(".gth-task-heading strong").textContent,
        environment: row.querySelector(".gth-environment").textContent,
        finishedAt: row.querySelector("time").getAttribute("datetime"),
        duration: row.querySelector(".gth-task-meta > span").getAttribute("title"),
      })));
      const beforeFailure = await completionProjection();
      assert(beforeFailure.length > 0, "the network exercise requires actual retained completion records");
      const failure = page.waitForEvent("requestfailed", {
        predicate: request => new URL(request.url()).pathname === "/api/v1/server-monitoring/gpu-tasks", timeout: 15_000,
      });
      abortNextGpuTasks = true;
      await failure;
      const recovery = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/server-monitoring/gpu-tasks"
        && response.status() === 200, { timeout: 20_000 }).catch(() => null);
      await page.waitForTimeout(250);
      assert.equal(abortedGpuTaskRequests, 1, "only a single read may be intentionally interrupted");
      assert.deepEqual(await completionProjection(), beforeFailure, "a failed read preserves the actual last-successful task records");
      assert.equal(await taskPanel.locator(".gth-notice, .gth-retry, .gth-access-notice, .gth-access-retry").count(), 0,
        "an ordinary transport failure must not display a warning, retry button or authorization error");
      for (const badge of await taskPanel.locator(".gth-active-item .gth-kind").all()) {
        assert.equal((await badge.innerText()).trim(), "最近运行任务", "cached activity cannot be described as a current task");
      }
      await assertGpuTasks(1);
      const recoveredTasksResponse = await recovery;
      assert(recoveredTasksResponse, "automatic polling must recover with an actual authenticated 200 response");
      const recoveredTasks = await recoveredTasksResponse.json();
      assert.equal(recoveredTasks.node_id, inventory.node_id);
      assert.deepEqual([...recoveredTasks.gpu_uuids].sort(), inventory.gpus.map(gpu => gpu.uuid).sort());
      await page.waitForTimeout(250);
      await assertGpuTasks(1);
      report.gpu_task_read_recovery = { aborted_gets: abortedGpuTaskRequests, retained_records: beforeFailure.length,
        warning_absent: "passed", cached_activity_label: "passed", recovered_http_status: recoveredTasksResponse.status(),
        recovered_history_status: recoveredTasks.status, recovered_activity_status: recoveredTasks.activity.status };
      if (values["exercise-collector"]) {
        const environment = { ...process.env, XDG_RUNTIME_DIR: `/run/user/${process.getuid()}`,
          DBUS_SESSION_BUS_ADDRESS: `unix:path=/run/user/${process.getuid()}/bus` };
        const unit = "nexpoly-server-monitoring-collector.service";
        const control = action => execFileSync("systemctl", ["--user", action, unit], { env: environment, stdio: "pipe", timeout: 15_000 });
        const command = execFileSync("systemctl", ["--user", "show", unit, "--property=ExecStart", "--value"], { env: environment, encoding: "utf8" });
        assert(command.includes(path.resolve(".runtime/server-monitoring-9003/venv/bin/python")) && command.includes("server_monitoring.collector"), "only this workspace's collector may be stopped");
        control("stop");
        try {
          await page.locator(".sm-status").filter({ hasText: "数据已过期" }).waitFor({ timeout: 30_000 });
          await page.screenshot({ path: path.join(output, "monitoring-stale.png"), fullPage: true });
          await page.locator(".sm-status").filter({ hasText: "采集不可用" }).waitFor({ timeout: 65_000 });
          await page.screenshot({ path: path.join(output, "monitoring-unavailable.png"), fullPage: true });
          assert.equal(await page.locator(".sm-gpu").count(), 4, "last observation remains labeled as old");
        } finally { control("start"); }
        await page.locator(".sm-status").filter({ hasText: "在线" }).waitFor({ timeout: 30_000 });
        const recovered = await (await context.request.get(new URL("/api/v1/server-monitoring/history", base).href, { headers })).json();
        assert.notEqual(recovered.collector_id, history.collector_id);
        assert(recovered.points.length <= 1, "restart begins a new history window");
        report.collector_recovery = "passed";
      }
      // A second actual browser tab must lose its metrics when the shared account logs out.
      const other = await context.newPage();
      await other.goto(new URL("/server-monitoring", base).href);
      await other.locator(".sm-status").filter({ hasText: "在线" }).waitFor();
      await logout(page, username);
      await other.getByTestId("server-monitoring-page").getByRole("button", { name: "登录账号", exact: true }).waitFor({ timeout: 15_000 });
      assert.equal(await other.locator(".sm-gpu").count(), 0);
      assert.equal(await other.getByRole("complementary", { name: "主机信息", exact: true }).count(), 0);
      assert.equal(await other.locator(".gth-task").count(), 0);
      assert.equal(await other.locator(".gth-active-item").count(), 0);
      await other.close();
    } else {
      await logout(page, username);
    }
    const revoked = await context.request.get(new URL("/api/v1/server-monitoring/snapshot", base).href, { headers });
    assert.equal(revoked.status(), 401);
    const revokedTasks = await context.request.get(new URL("/api/v1/server-monitoring/gpu-tasks", base).href, { headers });
    assert.equal(revokedTasks.status(), 401);
    assert(gpuTaskRequests > 0, "the page must read the real GPU task history endpoint");
    assert.deepEqual(pageErrors, [], "browser must not throw runtime errors");
    assert.equal(report.unexpected_usage_activity_requests.filter(request => request.username === username).length, 0, "monitoring must not request personal task activity");
    report.accounts.push({ username, status: "passed", gpu_count: 4, logical_cpu_count: 128,
      host_information: { metadata: "passed", disk_available: "passed", desktop_layout: "passed", mobile_layout: username === "zhiju000" ? "passed" : "not_repeated" },
      shared_theme: "passed", keyboard_gpu_selection: "passed", trend_selection: "passed", usage_activity_requests: 0,
      gpu_task_history: { status: "passed", running_tasks_only: "passed", technical_explanations_removed: "passed",
        api_history_status: gpuTasks.status, api_activity_status: gpuTasks.activity.status,
        api_freshness: gpuTasks.freshness, api_activity_age_seconds: activityAge, api_history_captured_at: gpuTasks.history_captured_at,
        observed_read_statuses: [...new Set(observedGpuTasks.map(({ data }) => `${data.status}/${data.activity.status}/${data.freshness}`))],
        aggregate_latest_completions: tasksForGpu(null).slice(0, 2).length, aggregate_gpu_navigation: "passed", scope_return: "passed", compact_desktop_layout: "passed",
        running_by_gpu: inventory.gpus.map(gpu => ({ index: gpu.index, count: runningForGpu(gpu.index).length })),
        hidden_non_task_activity: gpuTasks.activity.items.filter(item => item.kind !== "task" || item.status !== "running").length,
        retained_by_gpu: inventory.gpus.map(gpu => ({ index: gpu.index, count: tasksForGpu(gpu.index).length })),
        visible_row_limit: 2, source_coverage: gpuTasks.sources.map(source => ({ source: source.source_id, status: source.status,
          matched: source.matched_tasks, excluded: source.excluded_tasks, error_code: source.error_code })),
        browser_get_requests: gpuTaskRequests, guest_status: guestTasks.status(), mismatch_status: wrongTasks.status(), logout_status: revokedTasks.status() },
      persisted_history_points: after.points.length, collector_started_at: inventory.started_at,
      guest_status: guest.status(), mismatch_status: wrong.status(), logout_status: revoked.status() });
    await context.close();
  }
  assert.deepEqual(report.blocked_mutations, [], "monitoring page must not attempt compute mutations");
  assert.deepEqual(report.unexpected_usage_activity_requests, [], "monitoring must not fetch personal task records");
  report.status = "passed";
} catch (error) {
  report.status = "failed";
  // Avoid logging request bodies, session responses, URLs with credentials, or filled password fields.
  report.errors.push({ name: error.name, message: error.message.replaceAll(password, "[redacted]") });
  process.exitCode = 1;
} finally {
  await browser.close();
  report.completed_at = new Date().toISOString();
  await fs.writeFile(path.join(output, "result.json"), JSON.stringify(report, null, 2) + "\n", { mode: 0o600 });
  console.log(JSON.stringify(report));
}
