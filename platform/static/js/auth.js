document.querySelector("[data-auth-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const message = form.querySelector("[data-auth-message]");
  const button = form.querySelector('button[type="submit"]');
  const body = {username: form.elements.username.value.trim(), password: form.elements.password.value};
  if (form.dataset.mode === "register") body.confirm_password = form.elements.confirm_password.value;
  message.hidden = true;
  button.disabled = true;
  try {
    const response = await fetch(`/api/auth/${form.dataset.mode}`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content,
      },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(typeof result.detail === "string" ? result.detail : "输入不符合账号要求，请检查后重试。");
    }
    window.location.assign(form.dataset.next || "/");
  } catch (error) {
    message.textContent = error instanceof TypeError ? "服务连接失败，请检查本地服务是否运行。" : error.message;
    message.hidden = false;
  } finally {
    button.disabled = false;
  }
});
