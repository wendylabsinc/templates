/* Optional laptop USB connection diagnostics. Enabled by control.html?usb=1. */
(function (root) {
  // The headset reaches this page over adb reverse, so as localhost on the
  // plain-HTTP port that served the console (HTTP_PORT in the template).
  function usbPageUrl() {
    const port = root.location && root.location.port;
    return `http://localhost${port ? ':' + port : ''}/index.html`;
  }
  function recoveryStatus(link, robot, now = Date.now() / 1000) {
    if (robot.homing || robot.input_loss_recovery?.phase === 'returning')
      return {canArm:false, text:'Input lost — slowly returning to the saved home pose onboard. Teleop remains unarmed. Hold cancels the return.'};
    if (!link || !Number.isFinite(link.checked_at) || now - link.checked_at > 8)
      return {canArm:false, text:'USB monitor unavailable. Restart the laptop connection monitor; keep the robot holding.'};
    if (link.state === 'unauthorized')
      return {canArm:false, text:'Headset needs “Allow USB debugging”. In the headset, select “Always allow from this computer” and Allow. USB forwarding will recover after approval. Do not stop the G1 service to fix this.'};
    if (link.state !== 'ready' || !link.forwarded)
      return {canArm:false, text:`Headset USB: ${link.state}. Check the cable and connection. The monitor restores forwarding once the headset is authorized; it never rearms the robot.`};
    if (!robot.connected)
      return {canArm:false, text:'USB is ready, but the G1 controller is disconnected. Check its Ethernet link and onboard service. Go home requires that service to be running.'};
    if (!robot.xr_connected || !robot.hands_fresh)
      return {canArm:false, text:`USB is ready; waiting for fresh tracking. In the headset open ${usbPageUrl()} and enter VR. After recovery the robot remains unarmed; Go home remains available.`};
    const camera = robot.camera && !robot.camera.connected ? ' Camera is disconnected; restore its service before relying on the view.' : '';
    return {canArm:robot.state === 'HOLDING', text:'USB and tracking are ready. After a dropout, re-arm deliberately when the workspace is clear.' + camera};
  }
  if (typeof module !== 'undefined') module.exports = {recoveryStatus};
  if (typeof window === 'undefined') return;
  const enabled = new URLSearchParams(location.search).get('usb') === '1';
  let latest = null;
  async function refresh() {
    try {
      const response = await fetch('http://127.0.0.1:8202/status', {cache:'no-store', signal:AbortSignal.timeout(1500)});
      if (!response.ok) throw new Error('USB monitor unavailable');
      latest = await response.json();
    } catch (_) { latest = null; }
  }
  root.usbLink = {
    enabled,
    status: robot => recoveryStatus(latest, robot),
    async checkArm() {
      await refresh();
      const response = await fetch('/control/status', {cache:'no-store', signal:AbortSignal.timeout(1500)});
      return recoveryStatus(latest, await response.json());
    }
  };
  if (enabled) { refresh(); setInterval(refresh, 2000); }
})(typeof window === 'undefined' ? globalThis : window);
