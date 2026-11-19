// Shared operator switch for the desktop console and the basic control page.
export function bindWalkingControl(button, hint, post) {
  let permission = null, onboard = null, busy = false;
  function paint() {
    const known = typeof permission?.allowed === "boolean";
    button.disabled = busy || !known;
    button.classList.toggle("walking-allowed", permission?.allowed === true);
    button.setAttribute("aria-pressed", String(permission?.allowed === true));
    button.textContent = !known ? "Walking permission unavailable"
      : permission.allowed ? "Block walking" : "Allow walking";
    hint.textContent = !known ? "Waiting for the bridge."
      : !permission.allowed ? "Walking BLOCKED. Arms and waist remain available."
      : !onboard?.enabled ? "Walking allowed by operator; onboard walking is unavailable."
      : permission.awaiting_center ? "Walking allowed. Center the left stick before moving."
      : "Walking allowed while armed: left stick walks and turns.";
  }
  button.onclick = async () => {
    if (busy || !permission) return;
    busy = true; paint();
    let failure = null;
    try {
      permission = await post(!permission.allowed);
    } catch (error) {
      // Retain the last reported setting; a failed request is not a change.
      failure = error.message;
    } finally {
      busy = false;
      paint();
      if (failure) hint.textContent = "Walking permission request failed: " + failure;
    }
    // Errors remain visible until the next successful status poll.
  };
  paint();
  return (nextPermission, nextOnboard) => {
    permission = nextPermission || null;
    onboard = nextOnboard || null;
    paint();
  };
}
