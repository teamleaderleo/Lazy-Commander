import { createHash, randomUUID } from "node:crypto";
import { chmod, link, lstat, mkdir, open, readFile, unlink } from "node:fs/promises";
import { isAbsolute, join } from "node:path";

const RESULT_SCHEMA = "lazy-carrier-turn-result/v2";
const OPERATION_RECEIPT_SCHEMA = "lazy-carrier-operation-receipt/v1";
const LANE_BINDING_SCHEMA = "lazy-carrier-lane-binding/v1";
const ARTIFACT_SCHEMA = "lazy-carrier-private-artifact/v1";
const CONVERSATION_RE = /\/c\/([a-z0-9-]{16,})/i;
const SHA256_RE = /^[0-9a-f]{64}$/;
const PRIVATE_RESPONSE_SINKS = new WeakSet();

class BrowserCallTimeout extends Error {
  constructor() {
    super("browser call exceeded its explicit deadline");
    this.name = "BrowserCallTimeout";
  }
}

function requireString(value, name, { allowEmpty = false } = {}) {
  if (typeof value !== "string" || (!allowEmpty && value.length === 0)) {
    throw new TypeError(`${name} must be ${allowEmpty ? "a" : "a non-empty"} string`);
  }
  return value;
}

function requireNonnegativeInteger(value, name) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new TypeError(`${name} must be a non-negative safe integer`);
  }
  return value;
}

function promptIdentity(prompt) {
  return {
    characters: [...prompt].length,
    sha256: createHash("sha256").update(prompt, "utf8").digest("hex"),
  };
}

export function carrierOperationMarker(operationId) {
  requireString(operationId, "operationId");
  const digest = createHash("sha256").update(operationId, "utf8").digest("hex").slice(0, 24);
  return `[lazy-carrier-operation:${digest}]`;
}

function conversationRef(url) {
  const match = CONVERSATION_RE.exec(url);
  return match ? `chatgpt:${match[1]}` : null;
}

function validateWaits(maxWaitMs, pollMs, callTimeoutMs) {
  if (!Number.isInteger(maxWaitMs) || maxWaitMs < 0 || maxWaitMs > 55_000) {
    throw new RangeError("maxWaitMs must be an explicit integer from 0 to 55000");
  }
  if (!Number.isInteger(pollMs) || pollMs < 50 || pollMs > 5000) {
    throw new RangeError("pollMs must be an integer from 50 to 5000");
  }
  if (!Number.isInteger(callTimeoutMs) || callTimeoutMs < 50 || callTimeoutMs > 10_000) {
    throw new RangeError("callTimeoutMs must be an explicit integer from 50 to 10000");
  }
}

function validateTab(tab) {
  if (!tab || !tab.playwright || typeof tab.goto !== "function" || typeof tab.url !== "function") {
    throw new TypeError("tab must be a bound browser-client tab");
  }
}

function validateLaneBinding(binding, receipt, projectUrl) {
  if (!binding || binding.schema !== LANE_BINDING_SCHEMA) {
    throw new TypeError(`laneBinding.schema must be ${LANE_BINDING_SCHEMA}`);
  }
  requireString(binding.laneRef, "laneBinding.laneRef");
  requireNonnegativeInteger(binding.laneGeneration, "laneBinding.laneGeneration");
  requireString(binding.projectRef, "laneBinding.projectRef");
  requireString(binding.projectUrl, "laneBinding.projectUrl");
  requireString(binding.surfaceProfile, "laneBinding.surfaceProfile");
  if (binding.dedicated !== true || binding.owned !== true) {
    throw new TypeError("laneBinding must prove a dedicated owned lane");
  }
  if (!Array.isArray(binding.blockers) || binding.blockers.length !== 0) {
    throw new TypeError("laneBinding.blockers must be an empty array");
  }
  for (const name of ["laneRef", "laneGeneration", "projectRef", "surfaceProfile"]) {
    if (binding[name] !== receipt[name]) {
      throw new TypeError(`laneBinding.${name} does not match the durable operation`);
    }
  }
  if (projectUrl !== undefined && binding.projectUrl !== projectUrl) {
    throw new TypeError("laneBinding.projectUrl does not match projectUrl");
  }
}

function validateIssueAdmission(admission, operationId, prompt) {
  if (!admission || admission.schema !== OPERATION_RECEIPT_SCHEMA) {
    throw new TypeError(`admission.schema must be ${OPERATION_RECEIPT_SCHEMA}`);
  }
  const identity = promptIdentity(prompt);
  const marker = carrierOperationMarker(operationId);
  if (
    admission.operationId !== operationId ||
    admission.effectState !== "issued" ||
    admission.effectAttemptAdmitted !== true ||
    admission.next !== "attempt-send-once" ||
    admission.retryAuthorized !== false ||
    admission.authorizesEffects !== false ||
    admission.promptSha256 !== identity.sha256 ||
    admission.promptCharacters !== identity.characters ||
    !SHA256_RE.test(admission.bindingSha256 || "") ||
    !SHA256_RE.test(admission.gateReceiptSha256 || "") ||
    !prompt.includes(marker)
  ) {
    throw new TypeError("admission is not the first durable issue receipt for this exact prompt");
  }
  requireString(admission.attemptRef, "admission.attemptRef");
  return identity;
}

function validateResumeReceipt(receipt, operationId, expectedNext) {
  if (!receipt || receipt.schema !== OPERATION_RECEIPT_SCHEMA) {
    throw new TypeError(`resumeReceipt.schema must be ${OPERATION_RECEIPT_SCHEMA}`);
  }
  if (
    receipt.operationId !== operationId ||
    receipt.outcome !== "resume" ||
    receipt.next !== expectedNext ||
    receipt.sendAuthorized !== false ||
    receipt.observationOnly !== true ||
    receipt.retryAuthorized !== false ||
    receipt.authorizesEffects !== false
  ) {
    throw new TypeError(`resumeReceipt is not an exact ${expectedNext} receipt`);
  }
}

async function withCallTimeout(thunk, timeoutMs) {
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(thunk),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new BrowserCallTimeout()), timeoutMs);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

async function strictCount(locator, callTimeoutMs) {
  return await withCallTimeout(() => locator.count(), callTimeoutMs);
}

async function visibleConversationEvidence(tab, projectName, operationId, callTimeoutMs) {
  const url = await withCallTimeout(() => tab.url(), callTimeoutMs);
  const ref = conversationRef(url);
  const userHeading = tab.playwright.getByRole("heading", { name: "You said:" });
  const projectLink = tab.playwright.getByRole("link", {
    name: `Open ${projectName} project`,
  });
  const userMessages = tab.playwright.locator(
    '[data-message-author-role="user"] .whitespace-pre-wrap',
  );
  const userMessageCount = await strictCount(userMessages, callTimeoutMs);
  let operationMarkerVisible = false;
  if (userMessageCount === 1) {
    const userText = await withCallTimeout(
      () => userMessages.nth(0).innerText(),
      callTimeoutMs,
    );
    operationMarkerVisible = userText.includes(carrierOperationMarker(operationId));
  }
  return {
    ref,
    committed:
      Boolean(ref) &&
      (await strictCount(userHeading, callTimeoutMs)) > 0 &&
      (await strictCount(projectLink, callTimeoutMs)) > 0 &&
      operationMarkerVisible,
  };
}

async function responseState(tab, callTimeoutMs) {
  const stop = tab.playwright.getByRole("button", { name: "Stop answering" });
  const copy = tab.playwright.getByRole("button", { name: "Copy response" });
  const assistantHeading = tab.playwright.getByRole("heading", { name: "ChatGPT said:" });
  const generating = (await strictCount(stop, callTimeoutMs)) > 0;
  const completeControl = (await strictCount(copy, callTimeoutMs)) > 0;
  const assistantVisible = (await strictCount(assistantHeading, callTimeoutMs)) > 0;
  return { generating, completeControl, assistantVisible };
}

async function composerIsEmpty(textbox, callTimeoutMs) {
  const observed = await withCallTimeout(
    () =>
      textbox.evaluate((element) => ({
        value: typeof element.value === "string" ? element.value : "",
        text: element.innerText || element.textContent || "",
      })),
    callTimeoutMs,
  );
  return observed && observed.value.trim() === "" && observed.text.trim() === "";
}

function validateArtifact(metadata) {
  if (!metadata || metadata.schema !== ARTIFACT_SCHEMA) {
    throw new TypeError(`responseSink must return ${ARTIFACT_SCHEMA}`);
  }
  requireString(metadata.artifactRef, "artifactRef");
  requireNonnegativeInteger(metadata.artifactBytes, "artifactBytes");
  requireNonnegativeInteger(metadata.artifactCharacters, "artifactCharacters");
  if (!SHA256_RE.test(metadata.artifactSha256 || "")) {
    throw new TypeError("artifactSha256 must be lowercase SHA-256");
  }
  return {
    artifactRef: metadata.artifactRef,
    artifactSha256: metadata.artifactSha256,
    artifactBytes: metadata.artifactBytes,
    artifactCharacters: metadata.artifactCharacters,
  };
}

export function createPrivateFileResponseSink({ stateRoot, operationId }) {
  requireString(stateRoot, "stateRoot");
  requireString(operationId, "operationId");
  if (!isAbsolute(stateRoot)) {
    throw new TypeError("stateRoot must be an absolute task-owned path");
  }
  const operationDigest = createHash("sha256").update(operationId, "utf8").digest("hex");
  const artifactName = `${operationDigest}.response.txt`;
  const artifactPath = join(stateRoot, artifactName);
  const artifactRef = `private-carrier:${operationDigest}`;

  const sink = async ({ tab, operationId: observedOperationId, conversationRef: expectedConversationRef }) => {
    validateTab(tab);
    if (observedOperationId !== operationId) {
      throw new TypeError("response identity does not match the private sink binding");
    }
    requireString(expectedConversationRef, "conversationRef");
    const currentUrl = await tab.url();
    if (conversationRef(currentUrl) !== expectedConversationRef) {
      throw new TypeError("private sink conversation binding changed");
    }
    const streaming = tab.playwright.locator('button[data-testid="stop-button"]');
    if ((await streaming.count()) !== 0) {
      throw new TypeError("private sink refuses a streaming response");
    }
    const turns = tab.playwright.locator('[data-testid^="conversation-turn-"]');
    const turnCount = await turns.count();
    if (!Number.isSafeInteger(turnCount) || turnCount < 1) {
      throw new TypeError("private sink found no conversation turns");
    }
    const finalTurn = turns.nth(turnCount - 1);
    const roleLocators = finalTurn.locator('[data-message-author-role]');
    if ((await roleLocators.count()) !== 1) {
      throw new TypeError("private sink found ambiguous final-turn authorship");
    }
    if ((await roleLocators.nth(0).getAttribute("data-message-author-role")) !== "assistant") {
      throw new TypeError("private sink final turn is not an assistant response");
    }
    const proseLocators = finalTurn.locator(".markdown");
    const proseCount = await proseLocators.count();
    if (proseCount < 1) {
      throw new TypeError("private sink found no final assistant prose");
    }
    let content = await proseLocators.nth(proseCount - 1).innerText();
    requireString(content, "response content", { allowEmpty: true });
    const contentBuffer = Buffer.from(content, "utf8");
    const contentSha = createHash("sha256").update(contentBuffer).digest("hex");
    const metadata = {
      schema: ARTIFACT_SCHEMA,
      artifactRef,
      artifactSha256: contentSha,
      artifactBytes: contentBuffer.byteLength,
      artifactCharacters: [...content].length,
    };

    await mkdir(stateRoot, { recursive: true, mode: 0o700 });
    const rootStat = await lstat(stateRoot);
    if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
      throw new TypeError("stateRoot must be a real directory, not a symlink");
    }
    await chmod(stateRoot, 0o700);

    try {
      const existing = await readFile(artifactPath);
      const existingSha = createHash("sha256").update(existing).digest("hex");
      if (existingSha !== contentSha || existing.byteLength !== contentBuffer.byteLength) {
        throw new TypeError("private response replay changed immutable content");
      }
      await chmod(artifactPath, 0o600);
      content = null;
      return metadata;
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }

    const temporaryPath = join(stateRoot, `.${artifactName}.${randomUUID()}.tmp`);
    let temporary;
    let staged = false;
    try {
      temporary = await open(temporaryPath, "wx", 0o600);
      await temporary.writeFile(contentBuffer);
      await temporary.sync();
      staged = true;
    } finally {
      await temporary?.close();
      if (!staged) {
        await unlink(temporaryPath).catch((error) => {
          if (error?.code !== "ENOENT") throw error;
        });
      }
    }

    try {
      await link(temporaryPath, artifactPath);
      await chmod(artifactPath, 0o600);
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      const existing = await readFile(artifactPath);
      const existingSha = createHash("sha256").update(existing).digest("hex");
      if (existingSha !== contentSha || existing.byteLength !== contentBuffer.byteLength) {
        throw new TypeError("concurrent private response replay changed immutable content");
      }
    } finally {
      await unlink(temporaryPath).catch((error) => {
        if (error?.code !== "ENOENT") throw error;
      });
    }
    content = null;
    return metadata;
  };
  PRIVATE_RESPONSE_SINKS.add(sink);
  return sink;
}

function responseShell(state = "not-observed") {
  return {
    state,
    characters: 0,
    artifactRef: null,
    artifactSha256: null,
    artifactBytes: null,
    rawContentEmitted: false,
  };
}

function baseResult(operationId, prompt, admission) {
  return {
    schema: RESULT_SCHEMA,
    operationId,
    bindingSha256: admission.bindingSha256,
    attemptRef: admission.attemptRef,
    laneRef: admission.laneRef,
    laneGeneration: admission.laneGeneration,
    prompt: promptIdentity(prompt),
    preflight: "pending",
    send: {
      attempted: false,
      settlement: "not-attempted",
      conversationRef: null,
      retryAuthorized: false,
    },
    response: responseShell(),
    errorCode: null,
    authorizesWork: false,
    authorizesEffects: false,
    authorizesDispatch: false,
  };
}

function observationResult(operationId, receipt) {
  return {
    schema: RESULT_SCHEMA,
    operationId,
    bindingSha256: receipt.bindingSha256,
    attemptRef: receipt.attemptRef,
    laneRef: receipt.laneRef,
    laneGeneration: receipt.laneGeneration,
    prompt: {
      sha256: receipt.promptSha256,
      characters: receipt.promptCharacters,
    },
    preflight: "not-applicable",
    send: {
      attempted: false,
      settlement: receipt.effectState,
      conversationRef: receipt.conversationRef,
      retryAuthorized: false,
    },
    response: responseShell(receipt.responseState),
    errorCode: null,
    authorizesWork: false,
    authorizesEffects: false,
    authorizesDispatch: false,
  };
}

async function persistCompleteResponse(result, tab, responseSink, callTimeoutMs) {
  try {
    const metadata = validateArtifact(
      await withCallTimeout(
        () =>
          responseSink({
            tab,
            operationId: result.operationId,
            conversationRef: result.send.conversationRef,
          }),
        callTimeoutMs,
      ),
    );
    result.response = {
      state: "complete",
      characters: metadata.artifactCharacters,
      ...metadata,
      rawContentEmitted: false,
    };
    return true;
  } catch {
    result.response = responseShell("unknown");
    result.errorCode = "private-response-capture-failed";
    return false;
  }
}

async function observeUntilBound({
  tab,
  projectName,
  result,
  responseSink,
  maxWaitMs,
  pollMs,
  callTimeoutMs,
  now,
  sleep,
  expectedConversationRef,
}) {
  const deadline = now() + maxWaitMs;
  while (true) {
    let evidence;
    try {
      evidence = await visibleConversationEvidence(
        tab,
        projectName,
        result.operationId,
        callTimeoutMs,
      );
    } catch {
      result.errorCode = "conversation-observation-failed";
      result.response.state = "unknown";
      return result;
    }
    if (expectedConversationRef && evidence.ref && evidence.ref !== expectedConversationRef) {
      result.errorCode = "conversation-binding-mismatch";
      result.response.state = "unknown";
      return result;
    }
    if (!evidence.committed) {
      if (now() >= deadline) {
        result.errorCode = expectedConversationRef
          ? "conversation-binding-timeout"
          : "send-settlement-timeout";
        result.response.state = "unknown";
        return result;
      }
      await sleep(Math.min(pollMs, Math.max(0, deadline - now())));
      continue;
    }
    result.send.settlement = "committed";
    result.send.conversationRef = evidence.ref;
    let state;
    try {
      state = await responseState(tab, callTimeoutMs);
    } catch {
      result.errorCode = "response-state-observation-failed";
      result.response.state = "unknown";
      return result;
    }
    if (!state.generating && state.completeControl && state.assistantVisible) {
      await persistCompleteResponse(result, tab, responseSink, callTimeoutMs);
      return result;
    }
    result.response.state = "pending";
    if (now() >= deadline) {
      result.errorCode = "response-pending-at-wait-bound";
      return result;
    }
    await sleep(Math.min(pollMs, Math.max(0, deadline - now())));
  }
}

export async function runCarrierTurn({
  tab,
  projectUrl,
  projectName,
  operationId,
  prompt,
  modelLabel,
  admission,
  laneBinding,
  responseSink,
  maxWaitMs,
  preflightWaitMs = 10_000,
  pollMs = 1000,
  callTimeoutMs = 5000,
  now = () => Date.now(),
  sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
}) {
  requireString(projectUrl, "projectUrl");
  requireString(projectName, "projectName");
  requireString(operationId, "operationId");
  requireString(prompt, "prompt");
  requireString(modelLabel, "modelLabel");
  if (typeof responseSink !== "function" || !PRIVATE_RESPONSE_SINKS.has(responseSink)) {
    throw new TypeError("responseSink must come from createPrivateFileResponseSink");
  }
  validateTab(tab);
  validateWaits(maxWaitMs, pollMs, callTimeoutMs);
  if (!Number.isInteger(preflightWaitMs) || preflightWaitMs < 0 || preflightWaitMs > 10_000) {
    throw new RangeError("preflightWaitMs must be an explicit integer from 0 to 10000");
  }
  validateIssueAdmission(admission, operationId, prompt);
  validateLaneBinding(laneBinding, admission, projectUrl);

  const result = baseResult(operationId, prompt, admission);
  try {
    const currentUrl = await withCallTimeout(() => tab.url(), callTimeoutMs);
    if (currentUrl !== projectUrl) {
      await withCallTimeout(() => tab.goto(projectUrl), callTimeoutMs);
    }
  } catch {
    result.preflight = "failed";
    result.errorCode = "project-navigation-failed";
    return result;
  }

  const textbox = tab.playwright.getByRole("textbox", {
    name: `New chat in ${projectName}`,
  });
  const model = tab.playwright.getByRole("button", { name: modelLabel });
  const preflightDeadline = now() + preflightWaitMs;
  while (true) {
    try {
      const textboxCount = await strictCount(textbox, callTimeoutMs);
      const modelCount = await strictCount(model, callTimeoutMs);
      if (textboxCount > 1 || modelCount > 1) {
        result.preflight = "failed";
        result.errorCode = "fresh-project-controls-ambiguous";
        return result;
      }
      if (textboxCount === 1 && modelCount === 1) {
        if (!(await composerIsEmpty(textbox, callTimeoutMs))) {
          result.preflight = "failed";
          result.errorCode = "composer-not-empty";
          return result;
        }
        break;
      }
    } catch {
      result.preflight = "failed";
      result.errorCode = "preflight-observation-failed";
      return result;
    }
    if (now() >= preflightDeadline) {
      result.preflight = "failed";
      result.errorCode = "fresh-project-composer-not-proven";
      return result;
    }
    await sleep(Math.min(pollMs, Math.max(0, preflightDeadline - now())));
  }

  try {
    await withCallTimeout(() => textbox.fill(prompt), callTimeoutMs);
  } catch {
    result.preflight = "failed";
    result.errorCode = "composer-fill-failed";
    return result;
  }
  const send = tab.playwright.getByRole("button", { name: "Send prompt" });
  try {
    if ((await strictCount(send, callTimeoutMs)) !== 1) {
      try {
        await withCallTimeout(() => textbox.fill(""), callTimeoutMs);
      } catch {
        // No send was attempted; cleanup failure grants no retry or effect authority.
      }
      result.preflight = "failed";
      result.errorCode = "send-control-not-proven";
      return result;
    }
  } catch {
    result.preflight = "failed";
    result.errorCode = "send-control-observation-failed";
    return result;
  }

  result.preflight = "passed";
  result.send.attempted = true;
  result.send.settlement = "ambiguous";
  try {
    await withCallTimeout(() => send.click(), callTimeoutMs);
  } catch {
    result.errorCode = "send-attempt-ambiguous";
    result.response.state = "unknown";
    return result;
  }

  return await observeUntilBound({
    tab,
    projectName,
    result,
    responseSink,
    maxWaitMs,
    pollMs,
    callTimeoutMs,
    now,
    sleep,
    expectedConversationRef: null,
  });
}

export async function observeCarrierTurn({
  tab,
  projectName,
  operationId,
  resumeReceipt,
  laneBinding,
  responseSink,
  maxWaitMs,
  pollMs = 1000,
  callTimeoutMs = 5000,
  now = () => Date.now(),
  sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
}) {
  requireString(projectName, "projectName");
  requireString(operationId, "operationId");
  if (typeof responseSink !== "function" || !PRIVATE_RESPONSE_SINKS.has(responseSink)) {
    throw new TypeError("responseSink must come from createPrivateFileResponseSink");
  }
  validateTab(tab);
  validateWaits(maxWaitMs, pollMs, callTimeoutMs);
  validateResumeReceipt(resumeReceipt, operationId, "observe-response-only");
  if (resumeReceipt.effectState !== "committed" || !resumeReceipt.conversationRef) {
    throw new TypeError("observe-response-only requires a committed conversation binding");
  }
  validateLaneBinding(laneBinding, resumeReceipt);
  const result = observationResult(operationId, resumeReceipt);
  return await observeUntilBound({
    tab,
    projectName,
    result,
    responseSink,
    maxWaitMs,
    pollMs,
    callTimeoutMs,
    now,
    sleep,
    expectedConversationRef: resumeReceipt.conversationRef,
  });
}

export async function reconcileCarrierEffect({
  tab,
  projectName,
  operationId,
  resumeReceipt,
  laneBinding,
  callTimeoutMs = 5000,
}) {
  requireString(projectName, "projectName");
  requireString(operationId, "operationId");
  validateTab(tab);
  validateResumeReceipt(resumeReceipt, operationId, "reconcile-effect-only");
  if (!["issued", "ambiguous"].includes(resumeReceipt.effectState)) {
    throw new TypeError("reconcile-effect-only requires an issued or ambiguous effect");
  }
  validateLaneBinding(laneBinding, resumeReceipt);
  if (!Number.isInteger(callTimeoutMs) || callTimeoutMs < 50 || callTimeoutMs > 10_000) {
    throw new RangeError("callTimeoutMs must be an explicit integer from 50 to 10000");
  }
  const result = observationResult(operationId, resumeReceipt);
  result.send.settlement = "ambiguous";
  result.response.state = "unknown";
  try {
    const evidence = await visibleConversationEvidence(
      tab,
      projectName,
      operationId,
      callTimeoutMs,
    );
    if (evidence.committed) {
      result.send.settlement = "committed";
      result.send.conversationRef = evidence.ref;
      result.errorCode = null;
    } else {
      result.errorCode = "effect-remains-ambiguous";
    }
  } catch {
    result.errorCode = "effect-reconciliation-observation-failed";
  }
  return result;
}
