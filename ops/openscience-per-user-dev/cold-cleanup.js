// The standard Agent bootstrap attaches the document's page grant to each request.
(() => {
  const el = id => document.getElementById(id);
  let directory = "", currentFile, cursor = "", nextName, offset = 0, busy = false;
  const previousNames = [];
  const previousOffsets = [];
  async function call(action, data) {
    const r = await fetch("/api/workspace-cleanup/" + action, {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data),
    });
    const result = await r.json();
    if (!r.ok) throw new Error(result.error);
    return result;
  }
  const messages = {
    cleanup_changed:"文件已更改，请返回目录重新查看。",
    cleanup_not_found:"文件已不存在，请返回目录重新查看。",
    cleanup_unavailable:"可以重试启动工作区了。",
    cleanup_expired:"此次操作已超时，请重新查看文件。",
    cleanup_busy:"正在处理其他文件操作，请稍后再试。",
  };
  function action(work) {
    return async () => {
      if (busy) return;
      busy = true;
      const controls = document.querySelectorAll("main button");
      for (const button of controls) button.disabled = true;
      el("status").textContent = "正在读取工作区状态…";
      try { await work(); }
      catch (error) {
        el("status").textContent = messages[error.message] || "未能确认操作结果。请刷新目录或重试启动工作区。";
      } finally {
        busy = false;
        for (const button of controls) button.disabled = false;
      }
    };
  }
  async function list(path, after = "") {
    const result = await call("list", {path, after});
    if (path !== directory || !after) previousNames.length = 0;
    directory = path;
    cursor = after;
    currentFile = undefined;
    el("preview").hidden = true;
    el("content").textContent = "";
    el("directory").hidden = false;
    el("folder").textContent = path || "项目目录";
    el("entries").replaceChildren();
    el("up").hidden = !path;
    nextName = result.next;
    el("more").hidden = !nextName;
    el("earlier").hidden = !after;
    for (const item of result.entries) {
      const row = document.createElement("li");
      const button = document.createElement("button");
      button.textContent = item.name;
      const name = path ? path + "/" + item.name : item.name;
      button.onclick = action(async () => {
        if (item.kind === "directory") return list(name);
        previousOffsets.length = 0;
        await view(name, 0);
      });
      row.append(button, item.kind === "directory" ? " 文件夹" : " " + item.size.toLocaleString() + " 字节");
      el("entries").append(row);
    }
    el("status").textContent = result.entries.length ? "" : "此目录没有可清理的文件。";
  }
  async function view(path, start, version) {
    const result = await call("read", {path, offset:start, ...(version && {version})});
    const bytes = Uint8Array.from(atob(result.content), ch => ch.charCodeAt(0));
    let decoded;
    try { decoded = new TextDecoder("utf-8",{fatal:true}).decode(bytes); } catch { /* render raw bytes */ }
    const binary = decoded === undefined || /[\u0000-\u0008\u000e-\u001f]/.test(decoded);
    el("content").textContent = binary
      ? Array.from(bytes, value => value.toString(16).padStart(2,"0")).join(" ") : decoded;
    el("filename").textContent = path;
    el("range").textContent = result.size
      ? `第 ${start + 1}–${start + bytes.length} 字节，共 ${result.size} 字节` + (binary ? "（十六进制）" : "")
      : "空文件";
    el("previous").hidden = start === 0;
    el("next").hidden = result.next === null;
    currentFile = {path, version:result.version, next:result.next};
    offset = start;
    el("directory").hidden = true;
    el("preview").hidden = false;
    el("status").textContent = "";
  }
  el("up").onclick = action(() => list(directory.split("/").slice(0,-1).join("/")));
  el("more").onclick = action(async () => {
    const last = cursor;
    await list(directory, nextName);
    previousNames.push(last);
  });
  el("earlier").onclick = action(async () => {
    await list(directory, previousNames.at(-1));
    previousNames.pop();
  });
  el("back").onclick = action(() => list(directory));
  el("next").onclick = action(async () => {
    const last = offset;
    await view(currentFile.path, currentFile.next, currentFile.version);
    previousOffsets.push(last);
  });
  el("previous").onclick = action(async () => {
    await view(currentFile.path, previousOffsets.at(-1), currentFile.version);
    previousOffsets.pop();
  });
  el("remove").onclick = () => {
    el("confirm-name").textContent = "删除后无法撤销：" + currentFile.path;
    el("confirmation").showModal();
  };
  el("cancel").onclick = () => el("confirmation").close();
  el("confirm").onclick = action(async () => {
    el("confirmation").close();
    await call("delete", {path:currentFile.path, version:currentFile.version});
    currentFile = undefined;
    el("preview").hidden = true;
    el("content").textContent = "";
    try {
      await list(directory);
      el("status").textContent = "文件已删除。磁盘空间仍然不足，可以继续清理。";
    } catch (error) {
      el("status").textContent = error.message === "cleanup_unavailable" ? "文件已删除。可以重试启动工作区。"
        : "文件已删除。目录更新失败，请重试启动工作区。";
    }
  });
  void action(() => list(""))();
})();
