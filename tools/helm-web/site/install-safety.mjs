export function isSelectedRecoveryDisconnect(transport, event) {
  return typeof transport?.ownsPort === "function" && transport.ownsPort(event?.target) === true;
}

export function claimInstallTerminalState(state, outcome) {
  if (outcome !== "complete" && outcome !== "unknown") {
    throw new TypeError(`invalid install terminal outcome: ${String(outcome)}`);
  }
  if (state.installComplete === true || state.persistentStateUnknown === true) {
    return false;
  }
  if (outcome === "complete") {
    state.installComplete = true;
  } else {
    state.persistentStateUnknown = true;
  }
  return true;
}
