/*
 * 单个来源的连接验证可能需要等待远端响应。提交后立即锁定按钮，
 * 让用户知道请求仍在进行，也避免重复提交造成“来源已存在”的误解。
 */
(() => {
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("form[data-source-submit]").forEach((form) => {
      const button = form.querySelector("[data-source-submit-button]");
      const status = form.querySelector("[data-source-submit-status]");
      if (!(button instanceof HTMLButtonElement) || !(status instanceof HTMLElement)) return;

      form.addEventListener("submit", (event) => {
        if (form.dataset.submitting === "true") {
          event.preventDefault();
          return;
        }
        if (!form.checkValidity()) return;

        form.dataset.submitting = "true";
        form.setAttribute("aria-busy", "true");
        button.disabled = true;
        button.textContent = form.dataset.pendingText || "正在处理…";
        status.hidden = false;
        status.textContent = "正在保存来源并验证连接，请不要重复点击。";
      });
    });
  });
})();
