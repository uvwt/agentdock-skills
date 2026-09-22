#!/usr/bin/env node
'use strict';

// AgentDock 安全适配：只保留请求签名，不加载用户 Home 目录中的更新代码，
// 不启动上游 wrapper 的后台更新/上报逻辑。
const crypto = require('crypto');
const core = require('./core/cliguard.js');
const originalFetch = globalThis.fetch;

function signedUrl(url) {
  const result = core.addCommonParams(url);
  return result && result.url ? result.url : String(url);
}

if (typeof originalFetch === 'function') {
  globalThis.fetch = async function agentDockSignedFetch(input, init) {
    if (!(typeof input === 'string' || input instanceof URL)) {
      return originalFetch(input, init);
    }

    const requestInit = Object.assign({}, init || {});
    const method = String(requestInit.method || 'GET').toUpperCase();
    const body = requestInit.body == null ? Buffer.alloc(0) : Buffer.from(String(requestInit.body));
    const bodyHash = crypto.createHash('md5').update(body.subarray(0, 16200)).digest('hex');
    const url = signedUrl(String(input));
    const headers = new Headers(requestInit.headers || {});
    const signHeaders = core.signRequest(method, url, bodyHash) || {};
    for (const [name, value] of Object.entries(signHeaders)) headers.set(name, value);
    requestInit.headers = headers;
    return originalFetch(url, requestInit);
  };
}

module.exports = {
  sign: core.signRequest,
  signRequest: core.signRequest,
  addCommonParams(url) { return signedUrl(url); }
};
