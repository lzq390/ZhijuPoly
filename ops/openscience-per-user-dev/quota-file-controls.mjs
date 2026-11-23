// The November editor already has read, edit and save. Mount this action next
// to its refresh control only on the quota-backed development deployment.
export function addDeleteFileControl(bar, before, selectedFile) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "删除文件";
  bar.insertBefore(button, before);
  button.addEventListener("click", () => {
    const {name, remove} = selectedFile();
    const dialog = document.createElement("dialog");
    dialog.setAttribute("aria-label", "删除文件");
    const question = document.createElement("p");
    question.textContent = "删除 " + name + "？";
    const cancel = document.createElement("button");
    cancel.textContent = "取消";
    cancel.autofocus = true;
    const confirm = document.createElement("button");
    confirm.textContent = "确认删除";
    const error = document.createElement("p");
    error.setAttribute("role", "alert");
    cancel.onclick = () => dialog.close();
    confirm.onclick = async () => {
      confirm.disabled = true;
      cancel.disabled = true;
      try {
        await remove();
        dialog.close();
      } catch (cause) {
        error.textContent = cause.message;
        confirm.disabled = false;
        cancel.disabled = false;
      }
    };
    dialog.addEventListener("close", () => dialog.remove(), {once: true});
    dialog.append(question, cancel, confirm, error);
    document.body.append(dialog);
    dialog.showModal();
  });
}
