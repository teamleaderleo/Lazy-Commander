import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  carrierOperationMarker,
  createPrivateFileResponseSink,
  observeCarrierTurn,
  reconcileCarrierEffect,
  runCarrierTurn,
} from "../scripts/carrier_turn.mjs";

const PROJECT = "https://chatgpt.com/g/g-p-test-lazy-legion/project";
const CHAT = "https://chatgpt.com/g/g-p-test-lazy-legion/c/12345678-1234-1234-1234-123456789abc";
const CHAT_REF = "chatgpt:12345678-1234-1234-1234-123456789abc";
const RESPONSE = "complete carrier response";
const OPERATION_ID = "test-operation";
const DEFAULT_PROMPT = `bounded prompt\n\n${carrierOperationMarker(OPERATION_ID)}`;
const sinkRoot = process.argv[2];
assert.ok(sinkRoot, "private sink test root is required");
const sharedPrivateSink = createPrivateFileResponseSink({
  stateRoot: sinkRoot,
  operationId: OPERATION_ID,
});

function sha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function receipt(prompt, overrides = {}) {
  return {
    schema: "lazy-carrier-operation-receipt/v1",
    outcome: "issued",
    operationId: OPERATION_ID,
    bindingSha256: "a".repeat(64),
    gateReceiptSha256: "b".repeat(64),
    routeRunId: "route-1",
    routeGeneration: 1,
    promptSha256: sha256(prompt),
    promptCharacters: [...prompt].length,
    projectRef: "project:lazy-legion-lab",
    laneRef: "lane:edge-primary",
    laneGeneration: 7,
    surfaceProfile: "chatgpt-project-v1",
    actionConfirmationRef: "confirmation:1",
    effectState: "issued",
    effectAttemptAdmitted: true,
    next: "attempt-send-once",
    attemptRef: "attempt:1",
    conversationRef: null,
    responseState: "not-observed",
    retryAuthorized: false,
    authorizesEffects: false,
    ...overrides,
  };
}

function laneBinding(overrides = {}) {
  return {
    schema: "lazy-carrier-lane-binding/v1",
    laneRef: "lane:edge-primary",
    laneGeneration: 7,
    projectRef: "project:lazy-legion-lab",
    projectUrl: PROJECT,
    surfaceProfile: "chatgpt-project-v1",
    dedicated: true,
    owned: true,
    blockers: [],
    ...overrides,
  };
}

class FakeLocator {
  constructor(tab, role, name) {
    this.tab = tab;
    this.role = role;
    this.name = name;
  }

  async count() {
    if (this.tab.scenario === "count-timeout" && this.role === "textbox") {
      return await new Promise(() => {});
    }
    if (this.role === "textbox") {
      if (this.tab.scenario === "preflight") return 0;
      if (this.tab.scenario === "delayed-preflight" && this.tab.step === 0) return 0;
      return 1;
    }
    if (this.role === "button" && this.name === "Extra High") return 1;
    if (this.role === "button" && this.name === "Send prompt") return 1;
    if (this.role === "button" && this.name === "Stop answering") {
      return this.tab.scenario === "pending" || (this.tab.scenario === "success" && this.tab.step === 0) ? 1 : 0;
    }
    if (this.role === "button" && this.name === "Copy response") {
      return this.tab.committed && this.tab.step > 0 ? 1 : 0;
    }
    if (this.role === "heading" && this.name === "You said:") return this.tab.committed ? 1 : 0;
    if (this.role === "heading" && this.name === "ChatGPT said:") return this.tab.committed ? 1 : 0;
    if (this.role === "link" && this.name === "Open Lazy Legion Lab project") return 1;
    return 0;
  }

  async fill(value) {
    this.tab.fills.push(value);
    this.tab.userText = value;
  }

  async click() {
    this.tab.clicks += 1;
    if (this.tab.scenario === "click-error") throw new Error("unknown send result");
    if (this.tab.scenario !== "settlement-timeout") {
      this.tab.committed = true;
      this.tab.currentUrl = CHAT;
    }
  }

  async evaluate() {
    if (this.role === "textbox") {
      const content = this.tab.scenario === "dirty-composer" ? "someone else's draft" : "";
      return { value: content, text: content };
    }
    return RESPONSE;
  }
}

class FakeDomLocator {
  constructor(tab, kind) {
    this.tab = tab;
    this.kind = kind;
  }

  async count() {
    if (this.kind === "streaming") return 0;
    if (this.kind === "turns") return 2;
    if (this.kind === "role" || this.kind === "prose" || this.kind === "user-prose") return 1;
    return 0;
  }

  nth() {
    if (this.kind === "turns") return new FakeDomLocator(this.tab, "final-turn");
    return this;
  }

  locator(selector) {
    if (this.kind === "final-turn" && selector === "[data-message-author-role]") {
      return new FakeDomLocator(this.tab, "role");
    }
    if (this.kind === "final-turn" && selector === ".markdown") {
      return new FakeDomLocator(this.tab, "prose");
    }
    return new FakeDomLocator(this.tab, "missing");
  }

  async getAttribute(name) {
    return this.kind === "role" && name === "data-message-author-role" ? "assistant" : null;
  }

  async innerText() {
    if (this.kind === "user-prose") return this.tab.userText;
    if (this.kind !== "prose") throw new Error("not prose");
    return this.tab.responseText;
  }
}

class FakeTab {
  constructor(scenario, { existingConversation = false, currentUrl = null } = {}) {
    this.scenario = scenario;
    this.currentUrl = currentUrl || (existingConversation ? CHAT : "about:blank");
    this.committed = existingConversation;
    this.clicks = 0;
    this.gotoCount = 0;
    this.fills = [];
    this.responseText = RESPONSE;
    this.userText = DEFAULT_PROMPT;
    this.step = existingConversation ? 1 : 0;
    this.clock = 0;
    this.playwright = {
      getByRole: (role, options) => new FakeLocator(this, role, options.name),
      locator: (selector) => {
        if (selector === 'button[data-testid="stop-button"]') return new FakeDomLocator(this, "streaming");
        if (selector === '[data-testid^="conversation-turn-"]') return new FakeDomLocator(this, "turns");
        if (selector === '[data-message-author-role="user"] .whitespace-pre-wrap') return new FakeDomLocator(this, "user-prose");
        return new FakeDomLocator(this, "missing");
      },
    };
  }

  async goto(url) {
    this.gotoCount += 1;
    this.currentUrl = url;
  }

  async url() {
    return this.currentUrl;
  }

  now = () => this.clock;

  sleep = async (milliseconds) => {
    this.clock += milliseconds;
    this.step += 1;
    if (this.scenario === "delayed-binding") {
      this.committed = true;
      this.currentUrl = CHAT;
    }
  };

}

async function run(scenario, overrides = {}) {
  const tab = new FakeTab(scenario, { currentUrl: overrides.currentUrl || null });
  const prompt = overrides.prompt || DEFAULT_PROMPT;
  const result = await runCarrierTurn({
    tab,
    projectUrl: PROJECT,
    projectName: "Lazy Legion Lab",
    operationId: OPERATION_ID,
    prompt,
    modelLabel: "Extra High",
    admission: overrides.admission || receipt(prompt),
    laneBinding: overrides.laneBinding || laneBinding(),
    responseSink: sharedPrivateSink,
    maxWaitMs: overrides.maxWaitMs ?? 100,
    preflightWaitMs: overrides.preflightWaitMs ?? 100,
    pollMs: 50,
    callTimeoutMs: overrides.callTimeoutMs ?? 50,
    now: tab.now,
    sleep: tab.sleep,
  });
  return { tab, result };
}

const largePrompt = `${"p".repeat(250_000)}\n\n${carrierOperationMarker(OPERATION_ID)}`;
const success = await run("success", { prompt: largePrompt });
assert.equal(success.result.preflight, "passed");
assert.equal(success.result.send.settlement, "committed");
assert.equal(success.result.send.conversationRef, CHAT_REF);
assert.equal(success.result.response.state, "complete");
assert.match(success.result.response.artifactRef, /^private-carrier:/);
assert.equal(success.result.prompt.characters, [...largePrompt].length);
assert.equal(success.tab.clicks, 1);
assert.equal("content" in success.result.response, false);
assert.equal(JSON.stringify(success.result).includes(RESPONSE), false);

const reusedProjectSurface = await run("success", { currentUrl: PROJECT });
assert.equal(reusedProjectSurface.result.preflight, "passed");
assert.equal(reusedProjectSurface.tab.gotoCount, 0);
assert.equal(reusedProjectSurface.tab.clicks, 1);

const delayedPreflight = await run("delayed-preflight");
assert.equal(delayedPreflight.result.preflight, "passed");
assert.equal(delayedPreflight.result.send.settlement, "committed");
assert.equal(delayedPreflight.tab.clicks, 1);

const pending = await run("pending");
assert.equal(pending.result.send.settlement, "committed");
assert.equal(pending.result.response.state, "pending");
assert.equal(pending.result.errorCode, "response-pending-at-wait-bound");
assert.equal(pending.result.send.retryAuthorized, false);
assert.equal(pending.tab.clicks, 1);

const ambiguous = await run("click-error");
assert.equal(ambiguous.result.send.settlement, "ambiguous");
assert.equal(ambiguous.result.response.state, "unknown");
assert.equal(ambiguous.result.errorCode, "send-attempt-ambiguous");
assert.equal(ambiguous.result.send.retryAuthorized, false);
assert.equal(ambiguous.tab.clicks, 1);

const preflight = await run("preflight");
assert.equal(preflight.result.preflight, "failed");
assert.equal(preflight.result.send.attempted, false);
assert.equal(preflight.result.errorCode, "fresh-project-composer-not-proven");
assert.equal(preflight.tab.clicks, 0);

const dirty = await run("dirty-composer");
assert.equal(dirty.result.errorCode, "composer-not-empty");
assert.equal(dirty.result.send.attempted, false);
assert.equal(dirty.tab.fills.length, 0);
assert.equal(dirty.tab.clicks, 0);

const settlementTimeout = await run("settlement-timeout");
assert.equal(settlementTimeout.result.send.settlement, "ambiguous");
assert.equal(settlementTimeout.result.response.state, "unknown");
assert.equal(settlementTimeout.result.errorCode, "send-settlement-timeout");
assert.equal(settlementTimeout.tab.clicks, 1);

const countTimeout = await run("count-timeout", { callTimeoutMs: 50 });
assert.equal(countTimeout.result.errorCode, "preflight-observation-failed");
assert.equal(countTimeout.tab.clicks, 0);

const refusedReplayTab = new FakeTab("success");
await assert.rejects(
  () =>
    runCarrierTurn({
      tab: refusedReplayTab,
      projectUrl: PROJECT,
      projectName: "Lazy Legion Lab",
      operationId: OPERATION_ID,
      prompt: DEFAULT_PROMPT,
      modelLabel: "Extra High",
      admission: receipt(DEFAULT_PROMPT, { effectAttemptAdmitted: false, outcome: "replay" }),
      laneBinding: laneBinding(),
      responseSink: sharedPrivateSink,
      maxWaitMs: 100,
      callTimeoutMs: 50,
    }),
  /first durable issue receipt/,
);
assert.equal(refusedReplayTab.gotoCount, 0);
assert.equal(refusedReplayTab.clicks, 0);

const missingGateTab = new FakeTab("success");
await assert.rejects(
  () =>
    runCarrierTurn({
      tab: missingGateTab,
      projectUrl: PROJECT,
      projectName: "Lazy Legion Lab",
      operationId: OPERATION_ID,
      prompt: DEFAULT_PROMPT,
      modelLabel: "Extra High",
      admission: receipt(DEFAULT_PROMPT, { gateReceiptSha256: undefined }),
      laneBinding: laneBinding(),
      responseSink: sharedPrivateSink,
      maxWaitMs: 100,
      callTimeoutMs: 50,
    }),
  /first durable issue receipt/,
);
assert.equal(missingGateTab.gotoCount, 0);
assert.equal(missingGateTab.clicks, 0);

const missingMarkerTab = new FakeTab("success");
await assert.rejects(
  () =>
    runCarrierTurn({
      tab: missingMarkerTab,
      projectUrl: PROJECT,
      projectName: "Lazy Legion Lab",
      operationId: OPERATION_ID,
      prompt: "prompt without operation marker",
      modelLabel: "Extra High",
      admission: receipt("prompt without operation marker"),
      laneBinding: laneBinding(),
      responseSink: sharedPrivateSink,
      maxWaitMs: 100,
      callTimeoutMs: 50,
    }),
  /first durable issue receipt/,
);
assert.equal(missingMarkerTab.gotoCount, 0);
assert.equal(missingMarkerTab.clicks, 0);

const observeTab = new FakeTab("observe", { existingConversation: true });
const observeResume = receipt(DEFAULT_PROMPT, {
  outcome: "resume",
  effectAttemptAdmitted: undefined,
  next: "observe-response-only",
  effectState: "committed",
  conversationRef: CHAT_REF,
  responseState: "pending",
  sendAuthorized: false,
  observationOnly: true,
});
const observed = await observeCarrierTurn({
  tab: observeTab,
  projectName: "Lazy Legion Lab",
  operationId: OPERATION_ID,
  resumeReceipt: observeResume,
  laneBinding: laneBinding(),
  responseSink: sharedPrivateSink,
  maxWaitMs: 100,
  pollMs: 50,
  callTimeoutMs: 50,
  now: observeTab.now,
  sleep: observeTab.sleep,
});
assert.equal(observed.response.state, "complete");
assert.equal(observed.send.attempted, false);
assert.equal(observeTab.gotoCount, 0);
assert.equal(observeTab.clicks, 0);
assert.equal(observeTab.fills.length, 0);
assert.equal(JSON.stringify(observed).includes(RESPONSE), false);

const reconcileTab = new FakeTab("reconcile", { existingConversation: true });
const reconcileResume = receipt(DEFAULT_PROMPT, {
  outcome: "resume",
  effectAttemptAdmitted: undefined,
  next: "reconcile-effect-only",
  effectState: "issued",
  responseState: "not-observed",
  sendAuthorized: false,
  observationOnly: true,
});
const reconciled = await reconcileCarrierEffect({
  tab: reconcileTab,
  projectName: "Lazy Legion Lab",
  operationId: OPERATION_ID,
  resumeReceipt: reconcileResume,
  laneBinding: laneBinding(),
  callTimeoutMs: 50,
});
assert.equal(reconciled.send.settlement, "committed");
assert.equal(reconciled.send.conversationRef, CHAT_REF);
assert.equal(reconciled.send.attempted, false);
assert.equal(reconcileTab.gotoCount, 0);
assert.equal(reconcileTab.clicks, 0);

const wrongMarkerTab = new FakeTab("reconcile", { existingConversation: true });
wrongMarkerTab.userText = "[lazy-carrier-operation:wrong]";
const wrongMarkerReconciliation = await reconcileCarrierEffect({
  tab: wrongMarkerTab,
  projectName: "Lazy Legion Lab",
  operationId: OPERATION_ID,
  resumeReceipt: reconcileResume,
  laneBinding: laneBinding(),
  callTimeoutMs: 50,
});
assert.equal(wrongMarkerReconciliation.send.settlement, "ambiguous");
assert.equal(wrongMarkerReconciliation.errorCode, "effect-remains-ambiguous");
assert.equal(wrongMarkerTab.clicks, 0);

const delayedObserveTab = new FakeTab("delayed-binding", { currentUrl: CHAT });
const delayedObserved = await observeCarrierTurn({
  tab: delayedObserveTab,
  projectName: "Lazy Legion Lab",
  operationId: OPERATION_ID,
  resumeReceipt: observeResume,
  laneBinding: laneBinding(),
  responseSink: sharedPrivateSink,
  maxWaitMs: 100,
  pollMs: 50,
  callTimeoutMs: 50,
  now: delayedObserveTab.now,
  sleep: delayedObserveTab.sleep,
});
assert.equal(delayedObserved.response.state, "complete");
assert.equal(delayedObserveTab.clicks, 0);
assert.equal(delayedObserveTab.fills.length, 0);

const privateFileSink = createPrivateFileResponseSink({
  stateRoot: sinkRoot,
  operationId: "sink-operation",
});
const privateSinkTab = new FakeTab("observe", { existingConversation: true });
const firstPrivateArtifact = await privateFileSink({
  tab: privateSinkTab,
  operationId: "sink-operation",
  conversationRef: CHAT_REF,
});
const replayedPrivateArtifact = await privateFileSink({
  tab: privateSinkTab,
  operationId: "sink-operation",
  conversationRef: CHAT_REF,
});
assert.deepEqual(replayedPrivateArtifact, firstPrivateArtifact);
assert.equal(firstPrivateArtifact.artifactSha256, sha256(RESPONSE));
await assert.rejects(
  async () => {
    privateSinkTab.responseText = "changed response";
    await privateFileSink({
      tab: privateSinkTab,
      operationId: "sink-operation",
      conversationRef: CHAT_REF,
    });
  },
  /changed immutable content/,
);

console.log(JSON.stringify({ schema: "lazy-carrier-turn-test/v2", testsPassing: 18 }));
