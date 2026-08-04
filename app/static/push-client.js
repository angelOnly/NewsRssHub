(() => {
  const controls = document.querySelector("[data-push-controls]");
  if (!controls) return;

  const stateNode = controls.querySelector("[data-push-state]");
  const statusNode = controls.querySelector("[data-push-status]");
  const messageNode = controls.querySelector("[data-push-message]");
  const enableButton = controls.querySelector("[data-push-enable]");
  const testButton = controls.querySelector("[data-push-test]");
  const disableButton = controls.querySelector("[data-push-disable]");
  const requestHeaders = {
    "Content-Type": "application/json",
    "X-NewsRSSHub-Push": "1",
  };
  let registration;
  let config;

  const setStatus = (message) => {
    if (statusNode) statusNode.textContent = message;
  };
  const setState = (message, state = "pending") => {
    if (!stateNode) return;
    stateNode.textContent = message;
    stateNode.classList.remove("healthy", "error", "pending");
    stateNode.classList.add(state);
  };
  const setMessage = (message, isError = false) => {
    if (!messageNode) return;
    messageNode.textContent = message;
    messageNode.classList.toggle("error", isError);
  };
  const supportsPush = () =>
    window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  const isStandalone = () =>
    window.navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
  const setButtons = (enabled) => {
    if (enableButton) enableButton.hidden = enabled;
    if (testButton) testButton.hidden = !enabled;
    if (disableButton) disableButton.hidden = !enabled;
  };
  const toApplicationServerKey = (value) => {
    const padded = `${value}${"=".repeat((4 - (value.length % 4)) % 4)}`;
    const binary = window.atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  };
  const requestJson = async (path, options = {}) => {
    const response = await fetch(path, {
      credentials: "same-origin",
      ...options,
      headers: { ...requestHeaders, ...(options.headers || {}) },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "请求失败，请稍后重试。");
    return payload;
  };

  const prepare = async () => {
    if (!supportsPush()) {
      setState("不支持", "error");
      setStatus("当前浏览器不支持系统通知，或网站未通过 HTTPS 打开。");
      setButtons(false);
      return false;
    }
    if (!isStandalone()) {
      setState("需从主屏幕打开");
      setStatus("请先从 Safari 的分享菜单添加到主屏幕，再从主屏幕图标打开。");
      setButtons(false);
      return false;
    }
    registration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    await navigator.serviceWorker.ready;
    config = await requestJson("/api/push/config", { method: "GET" });
    if (!config.available || !config.public_key) {
      setState("暂不可用", "error");
      setStatus(config.message || "手机通知暂不可用。");
      setButtons(false);
      return false;
    }
    return true;
  };

  const saveSubscription = async (subscription) => {
    const payload = await requestJson("/api/push/subscription", {
      method: "POST",
      body: JSON.stringify(subscription.toJSON()),
    });
    setState("已开启", "healthy");
    setStatus(payload.status?.message || "手机通知已开启。");
    setButtons(true);
  };

  const refresh = async () => {
    try {
      if (!(await prepare())) return;
      const subscription = await registration.pushManager.getSubscription();
      if (subscription && Notification.permission === "granted") {
        await saveSubscription(subscription);
      } else {
        const denied = Notification.permission === "denied";
        setState(denied ? "未授权" : "未开启", "pending");
        setStatus(denied ? "这台手机尚未获准通知；请在 iPhone 设置中重新开启。" : "这台手机尚未开启通知。");
        setButtons(false);
      }
    } catch (error) {
      setState("检查失败", "error");
      setStatus(error.message || "无法检查手机通知状态。");
      setButtons(false);
    }
  };

  enableButton?.addEventListener("click", async () => {
    try {
      if (!(await prepare())) return;
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        setState("未授权", "error");
        setStatus("你尚未允许通知；可在 iPhone 的设置中重新开启。");
        setButtons(false);
        return;
      }
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: toApplicationServerKey(config.public_key),
        });
      }
      await saveSubscription(subscription);
      setMessage("手机通知已开启。你可以发送一条测试通知确认效果。");
    } catch (error) {
      setMessage(error.message || "开启手机通知失败，请稍后重试。", true);
    }
  });

  testButton?.addEventListener("click", async () => {
    try {
      await requestJson("/api/push/test", { method: "POST", body: "{}" });
      setMessage("测试通知已发送，请稍候查看系统通知。");
    } catch (error) {
      setMessage(error.message || "测试通知发送失败。", true);
    }
  });

  disableButton?.addEventListener("click", async () => {
    try {
      const currentRegistration = registration || (await navigator.serviceWorker.getRegistration("/"));
      const subscription = currentRegistration && (await currentRegistration.pushManager.getSubscription());
      if (subscription) await subscription.unsubscribe();
      await requestJson("/api/push/subscription", { method: "DELETE", body: "{}" });
      setState("已关闭", "pending");
      setStatus("这台手机的通知已关闭。");
      setButtons(false);
    } catch (error) {
      setMessage(error.message || "关闭手机通知失败。", true);
    }
  });

  refresh();
})();
