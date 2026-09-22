#!/usr/bin/env node
/**
 * meituan-coupon-assistant 统一入口脚本（领券专用）
 *
 * 本脚本是 meituan-living-assistant 的领券子集，仅保留领券相关能力，
 * 移除了导购下单（search / order / location / hotword / check-wx-ai-pay / check-login）。
 *
 * 跨平台（macOS / Windows）统一调度，AI 只需执行:
 *   node run.js <command> [options]
 *
 * 子命令:
 *   execute --output response     初始化并进入领券或授权分支
 *   init                          环境初始化（运行环境与内置认证组件检查）
 *   get-client-type               检测当前运行环境（pc/mac/windows/miniprogram）
 *   get-device-token              获取设备标识
 *   get-token [--env test|prod]   获取缓存的用户Token
 *   auth-prepare [--env test|prod]  准备授权链接与二维码
 *   auth-get-code [--env test|prod]  获取授权链接
 *   auth-poll-token               获取用户授权结果
 *   auth-complete [--output response]  等待授权并立即领券
 *   qrcode <url>                  获取二维码图片URL（服务端生成）
 *   issue [--output response]       领券
 *   logout                        退出登录
 *   clear-device-token            清除设备标识
 *
 * 所有命令输出 JSON 到 stdout，错误信息输出到 stderr。
 */


const { execSync, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const https = require('https');

// ── 全局常量 ─────────────────────────────────────────────────
const SCRIPTS_DIR = __dirname;
const SKILL_DIR = path.dirname(SCRIPTS_DIR);
const CLIENT_ID = 'c6f50b5a1e2f4e2bb00a3e2f58df3ced';
const PT_PASSPORT_BIN = path.join(SCRIPTS_DIR, 'vendor', 'pt-passport', 'dist', 'index.js');
const STATE_HOME = path.resolve((process.env.MEITUAN_COUPON_DATA_DIR || '').trim() || '.');
const HAS_STATE_HOME = Boolean((process.env.MEITUAN_COUPON_DATA_DIR || '').trim());
const AUTH_DIR = path.join(STATE_HOME, 'credentials');
const PASSPORT_HOME = path.join(STATE_HOME, 'home');
const PASSPORT_TMP = path.join(STATE_HOME, 'tmp');
const QR_CODE_TIMEOUT_MS = 5000;
const QR_CODE_MAX_ATTEMPTS = 2;
const PYTHON = findPython();
const TERMINAL_STATE = 'TERMINAL';
const AUTH_REQUIRED_STATE = 'AUTH_REQUIRED';
const REAUTH_REQUIRED_STATE = 'REAUTH_REQUIRED';

function sanitizeMarkdownCell(value) {
  return String(value === undefined || value === null ? '' : value)
      .replace(/[\r\n]+/g, ' ')
      .replace(/\\/g, '\\\\')
      .replace(/([|`*_[\]<>])/g, '\\$1');
}

function couponTable(displayCoupons) {
  if (!Array.isArray(displayCoupons) || displayCoupons.length === 0) return '';
  const rows = displayCoupons.slice(0, 8).map(function (coupon) {
    return '| ' + sanitizeMarkdownCell(coupon && coupon.name) +
        ' | ' + sanitizeMarkdownCell(coupon && coupon.discount_info) +
        ' | ' + sanitizeMarkdownCell(coupon && coupon.valid_period) + ' |';
  });
  return [
    '| 券名称 | 满减信息 | 有效期 |',
    '|--------|---------|--------|',
    ...rows
  ].join('\n');
}

function issueResponseText(result) {
  const couponCount = Number(result && result.coupon_count) || 0;
  const table = couponTable(result && result.display_coupons);

  if (result && result.cached === true) {
    if (couponCount === 0) return '本次没有返回可领取的美团优惠券。';
    if (!table) return '您今天已经领过美团优惠券，优惠券详情可在美团 App 查看。';
    return '您今天已经领过美团的优惠券啦，这是今日领取的优惠详情：\n\n' +
        table + '\n\n优惠券详情可在美团 App 查看。';
  }
  if (result && result.code === 1014) {
    return '您今天已经领取过美团优惠券，无需重复领取。';
  }
  if (result && result.success === true && couponCount > 0) {
    const countStr = sanitizeMarkdownCell(result.count_str);
    const summary = '🎉 一键领券完成！本次共领取 ' + couponCount +
        ' 张美团优惠券' + (countStr ? '，包括' + countStr : '') + '！';
    return summary + (table ? '\n\n' + table : '') +
        '\n\n以上是部分优惠信息，可以在美团 App「我的 → 红包卡券」查看所有券详情。';
  }
  if (result && result.success === true && couponCount === 0) {
    return '本次没有返回可领取的美团优惠券。';
  }
  if (result && result.code === 401) return '登录已过期，请重新登录';
  if (result && (result.code === 509 || result.code === 50200)) {
    return '请求过于频繁，请稍后再试';
  }
  if (result && result.code === 2213) return '服务暂时开小差了，请稍后再试';
  if (result && (result.error === 'TIMEOUT' || result.error === 'NETWORK_ERROR')) {
    return '服务暂时开小差了，请先核对账户状态后再决定是否重试 🔧';
  }
  if (result && result.error === 'NO_TOKEN') return '登录已过期，请重新登录';
  return '服务暂时开小差了，请稍后再试 🔧';
}

function setupResponseText(error) {
  const messages = {
    PATH_NOT_FOUND: 'Skill 脚本目录未找到，请尝试重新安装本 Skill。',
    PYTHON_NOT_FOUND: '本 Skill 需要 Python 3，请安装后重试。',
    PYTHON_VERSION_2: '本 Skill 需要 Python 3，请安装后重试。',
    NODE_VERSION_LOW: '当前 Node.js 版本过低，本 Skill 需要 >= 18。请升级后重试。',
    DATA_DIR_REQUIRED: '本 Skill 尚未配置独立数据目录。',
    BUNDLE_NOT_FOUND: '认证组件缺失，请尝试重新安装本 Skill。'
  };
  return messages[error] || '服务暂时开小差了，请稍后再试 🔧';
}

function terminalResponse(responseText) {
  return { state: TERMINAL_STATE, response_text: responseText };
}

// 动态获取 certifi 证书路径，用于修复 macOS Python SSL 证书问题
// 若 certifi 未安装则为空字符串，Python 脚本使用系统默认证书
const CERT_FILE = (() => {
  try {
    return execSync(`${PYTHON} -m certifi`, { encoding: 'utf-8', timeout: 5000, stdio: 'pipe' }).trim();
  } catch (_) { return ''; }
})();

// ── 工具函数 ─────────────────────────────────────────────────

function findPython() {
  for (const cmd of ['python3', 'python']) {
    try {
      const ver = execSync(`${cmd} --version`, { encoding: 'utf-8', timeout: 10000, stdio: 'pipe' }).trim();
      if (ver && !ver.startsWith('Python 2.')) return cmd;
    } catch (_) { /* ignore */ }
  }
  return 'python3';
}

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

function fail(error, extra) {
  out(Object.assign({ ok: false, error }, extra || {}));
  process.exit(1);
}

/** 执行 Python 脚本，返回解析后的 JSON */
function runPython(scriptName, args) {
  const scriptPath = path.join(SCRIPTS_DIR, scriptName);
  const cmdArgs = [scriptPath, ...args];
  try {
    try { fs.mkdirSync(AUTH_DIR, { recursive: true }); } catch (_) {}
    const sslEnv = CERT_FILE
        ? { SSL_CERT_FILE: CERT_FILE, REQUESTS_CA_BUNDLE: CERT_FILE }
        : {};
    const result = spawnSync(PYTHON, cmdArgs, {
      encoding: 'utf-8',
      timeout: 30000,
      stdio: ['pipe', 'pipe', 'pipe'],
      cwd: SCRIPTS_DIR,
      env: Object.assign({}, process.env, sslEnv, {
        MEITUAN_COUPON_AUTH_FILE: path.join(AUTH_DIR, 'token.json'),
        NODE_OPTIONS: ''
      })
    });
    const stdout = (result.stdout || '').trim();
    if (result.status !== 0) {
      try { return JSON.parse(stdout); } catch (_) {}
      return { ok: false, error: 'SCRIPT_ERROR', message: (result.stderr || stdout || 'Unknown error').trim() };
    }
    try { return JSON.parse(stdout); } catch (_) {
      return { ok: false, error: 'PARSE_ERROR', message: 'Invalid JSON from script', raw: stdout };
    }
  } catch (e) {
    return { ok: false, error: 'EXEC_ERROR', message: e.message };
  }
}

/** 执行 pt-passport CLI 命令，返回原始 stdout */
function runPassport(args) {
  try {
    if (!HAS_STATE_HOME) return { exitCode: 1, stdout: '', stderr: 'DATA_DIR_REQUIRED' };
    for (const dir of [STATE_HOME, AUTH_DIR, PASSPORT_HOME, PASSPORT_TMP]) {
      fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    }
    const result = spawnSync(process.execPath, [PT_PASSPORT_BIN, ...args], {
      encoding: 'utf-8',
      timeout: 620000,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: Object.assign({}, process.env, {
        HOME: PASSPORT_HOME,
        TMPDIR: PASSPORT_TMP,
        PT_PASSPORT_AUTH_FILE: path.join(AUTH_DIR, 'pt_passport_auth.json'),
        NODE_OPTIONS: ''
      }),
      shell: false
    });
    try { fs.chmodSync(path.join(AUTH_DIR, 'pt_passport_auth.json'), 0o600); } catch (_) {}
    return {
      exitCode: result.status,
      stdout: (result.stdout || '').trim(),
      stderr: (result.stderr || '').trim()
    };
  } catch (e) {
    return { exitCode: 1, stdout: '', stderr: e.message };
  }
}

/**
 * 从 pt-passport 缓存中读取当前用户 Token
 * 内部使用，不暴露到命令行参数中，防止 token 泄漏
 */
function getCachedToken() {
  const res = runPassport(['get-token', '--client_id', CLIENT_ID]);
  if (res.exitCode === 0 && res.stdout) {
    return res.stdout;
  }
  return null;
}

/** 解析 --key value 形式的命令行参数 */
function parseArgs(argv) {
  const args = {};
  const positional = [];
  for (let i = 0; i < argv.length; i++) {
    if (argv[i].startsWith('--')) {
      const key = argv[i].slice(2);
      if (i + 1 < argv.length && !argv[i + 1].startsWith('--')) {
        args[key] = argv[++i];
      } else {
        args[key] = 'true';
      }
    } else {
      positional.push(argv[i]);
    }
  }
  return { args, positional };
}



// ── 子命令实现 ───────────────────────────────────────────────

const commands = {};

/**
 * init — 环境初始化
 */
commands.init = function () {
  if (!fs.existsSync(SCRIPTS_DIR) || !fs.statSync(SCRIPTS_DIR).isDirectory()) {
    fail('PATH_NOT_FOUND');
  }
  if (!HAS_STATE_HOME) fail('DATA_DIR_REQUIRED');

  let pyVer = '';
  try {
    pyVer = execSync(`${PYTHON} --version`, { encoding: 'utf-8', timeout: 10000, stdio: 'pipe' }).trim();
  } catch (_) { /* ignore */ }
  if (!pyVer) fail('PYTHON_NOT_FOUND');
  if (pyVer.startsWith('Python 2.')) fail('PYTHON_VERSION_2');

  const nodeMajor = parseInt(process.versions.node.split('.')[0], 10);
  if (nodeMajor < 18) fail('NODE_VERSION_LOW', { current: String(nodeMajor), required: '>=18' });
  if (!fs.existsSync(PT_PASSPORT_BIN)) fail('BUNDLE_NOT_FOUND');

  for (const dir of [STATE_HOME, AUTH_DIR, PASSPORT_HOME, PASSPORT_TMP]) {
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    try { fs.chmodSync(dir, 0o700); } catch (_) {}
  }

  var envType = (process.env.MEITUAN_COUPON_CLIENT_TYPE || '').toLowerCase();
  var clientType = envType === 'mac' || envType === 'windows' || envType === 'miniprogram'
      ? envType
      : process.platform === 'darwin' ? 'mac' : process.platform === 'win32' ? 'windows' : 'pc';
  out({ ok: true, bundled_passport: true, clientType: clientType });
};

/**
 * get-client-type — 检测当前运行环境
 * 返回: pc / mac / windows / miniprogram
 */
commands['get-client-type'] = function () {
  var envType = (process.env.MEITUAN_COUPON_CLIENT_TYPE || '').toLowerCase();
  if (envType === 'mac' || envType === 'windows' || envType === 'miniprogram') {
    out({ ok: true, clientType: envType });
    return;
  }
  if (envType === 'pc') {
    out({ ok: true, clientType: process.platform === 'darwin' ? 'mac' : 'windows' });
    return;
  }
  // 操作系统推断
  if (process.platform === 'darwin') {
    out({ ok: true, clientType: 'mac' });
  } else if (process.platform === 'win32') {
    out({ ok: true, clientType: 'windows' });
  } else if (process.platform === 'linux') {
    out({ ok: true, clientType: 'pc' });
  } else {
    out({ ok: true, clientType: 'pc' });
  }
};

/**
 * get-device-token — 获取设备标识
 */
commands['get-device-token'] = function () {
  const result = runPython('auth.py', ['get-device-token']);
  if (result.success && result.device_token) {
    out({ ok: true, configured: true });
  } else if (result.device_token) {
    out({ ok: true, configured: true });
  } else {
    fail('DEVICE_TOKEN_FAILED', { detail: result });
  }
};

/**
 * get-token — 获取缓存的用户 Token
 */
commands['get-token'] = function (argv) {
  const { args } = parseArgs(argv || []);
  const passportArgs = ['get-token', '--client_id', CLIENT_ID];
  if (args['env'] === 'test') {
    passportArgs.push('--env', 'test');
  }
  const res = runPassport(passportArgs);
  if (res.exitCode === 0 && res.stdout) {
    out({ ok: true });
  } else {
    out({ ok: false, error: 'NO_TOKEN', message: 'Token not found or expired' });
  }
};

/**
 * auth-get-code — 获取授权链接
 */
function getAuthCodeResult(argv) {
  const { args } = parseArgs(argv || []);
  const passportArgs = ['auth', 'get-code', '--client_id', CLIENT_ID];
  if (args['env'] === 'test') {
    passportArgs.push('--env', 'test');
  }
  const res = runPassport(passportArgs);
  const stdout = res.stdout;

  // Token: <token> — 缓存命中
  const tokenMatch = stdout.match(/Token:\s*(.+)/);
  if (tokenMatch) {
    return { ok: true, type: 'token', token: tokenMatch[1].trim() };
  }

  // AUTH_LINK: <url>
  const linkMatch = stdout.match(/AUTH_LINK:\s*(.+)/);
  if (linkMatch) {
    return { ok: true, type: 'auth_link', url: linkMatch[1].trim() };
  }

  // ❌ 错误
  const errorMatch = stdout.match(/❌\s*code=(\d+)\s*message=(.*)/);
  if (errorMatch) {
    return { ok: false, error: 'AUTH_ERROR', code: errorMatch[1], message: errorMatch[2].trim() };
  }

  return { ok: false, error: 'UNKNOWN', raw: stdout, stderr: res.stderr };
}

commands['auth-get-code'] = function (argv) {
  const result = getAuthCodeResult(argv);
  out(result && result.type === 'token' ? { ok: true, type: 'token' } : result);
};

/**
 * auth-poll-token — 获取用户授权结果
 * 注意：poll-token 从 get-code 生成的 session 文件读取环境信息，无需传 --env
 *
 * 兜底逻辑：若 poll-token 失败，只读取本地缓存二次确认，
 * 不调用 auth get-code，避免静默创建新的授权会话。
 */
function pollAuthTokenResult() {
  const res = runPassport(['auth', 'poll-token', '--client_id', CLIENT_ID]);
  const stdout = res.stdout;

  // 正常成功
  const tokenMatch = stdout.match(/Token:\s*(.+)/);
  if (res.exitCode === 0 && tokenMatch) {
    return { ok: true, token: tokenMatch[1].trim() };
  }

  // poll 失败，只检查现有缓存，不创建新会话
  const cachedToken = getCachedToken();
  if (cachedToken) {
    return { ok: true, token: cachedToken };
  }

  // 兜底未命中缓存：确认登录确实失败
  return { ok: false, error: 'POLL_FAILED', message: '登录失败，请重新登录' };
}

commands['auth-poll-token'] = function () {
  const result = pollAuthTokenResult();
  out(result.ok ? { ok: true } : result);
};

// ── CLIGuard 签名集成 ─────────────────────────────────────────

function loadCliguard() {
  const vendorPath = path.join(
      SCRIPTS_DIR, 'vendor', 'cliguard', 'core', 'cliguard.js'
  );
  if (fs.existsSync(vendorPath)) return require(vendorPath);
  return null;
}

function addCommonParams(urlStr) {
  try {
    const cliguard = loadCliguard();
    if (!cliguard || typeof cliguard.addCommonParams !== 'function') return urlStr;
    const result = cliguard.addCommonParams(urlStr);
    return (result && result.url) ? result.url : urlStr;
  } catch (e) {
    process.stderr.write('[run.js:addCommonParams] warning: ' + e.message + '\n');
    return urlStr;
  }
}

function makeSignHeaders(method, urlStr, bodyHash) {
  try {
    const cliguard = loadCliguard();
    if (!cliguard || typeof cliguard.signRequest !== 'function') return {};
    return cliguard.signRequest(method.toUpperCase(), urlStr, bodyHash || '') || {};
  } catch (e) {
    process.stderr.write('[run.js:makeSignHeaders] warning: ' + e.message + '\n');
    return {};
  }
}

function httpsPost(urlStr, bodyObj, extraHeaders, timeoutMs) {
  return new Promise(function (resolve, reject) {
    const bodyStr = JSON.stringify(bodyObj);
    const bodyBuf = Buffer.from(bodyStr, 'utf-8');
    const hashSlice = bodyBuf.slice(0, 16200);
    const bodyHash = crypto.createHash('md5').update(hashSlice).digest('hex');
    const signedUrl = addCommonParams(urlStr);
    const sigHeaders = makeSignHeaders('POST', signedUrl, bodyHash);
    const parsed = new URL(signedUrl);
    const options = {
      hostname: parsed.hostname,
      port: parsed.port || 443,
      path: parsed.pathname + parsed.search,
      method: 'POST',
      headers: Object.assign({
        'Content-Type': 'application/json',
        'Content-Length': bodyBuf.length,
        'X-Requested-With': 'XMLHttpRequest'
      }, sigHeaders, extraHeaders || {})
    };
    const req = https.request(options, function (res) {
      const chunks = [];
      res.on('data', function (chunk) { chunks.push(chunk); });
      res.on('end', function () {
        const body = Buffer.concat(chunks).toString('utf-8');
        try { resolve({ status: res.statusCode, data: JSON.parse(body) }); }
        catch (_) { resolve({ status: res.statusCode, data: null, raw: body }); }
      });
    });
    req.on('error', function (e) { reject(e); });
    req.setTimeout(timeoutMs || 15000, function () { req.destroy(); reject(new Error('TIMEOUT')); });
    req.write(bodyBuf);
    req.end();
  });
}

function requestQrCodeWithRetry(apiUrl, body, attempt) {
  return httpsPost(apiUrl, body, null, QR_CODE_TIMEOUT_MS)
      .then(function (resp) {
        const data = resp.data;
        if (data && data.data) return { imageUrl: data.data };
        if (attempt < QR_CODE_MAX_ATTEMPTS) {
          return requestQrCodeWithRetry(apiUrl, body, attempt + 1);
        }
        return { message: 'No image returned', raw: data };
      })
      .catch(function (error) {
        if (attempt < QR_CODE_MAX_ATTEMPTS) {
          return requestQrCodeWithRetry(apiUrl, body, attempt + 1);
        }
        return { message: error.message };
      });
}

function httpsGet(urlStr, extraHeaders) {
  return new Promise(function (resolve, reject) {
    const signedUrl = addCommonParams(urlStr);
    const sigHeaders = makeSignHeaders('GET', signedUrl, '');
    const parsed = new URL(signedUrl);
    const options = {
      hostname: parsed.hostname,
      port: parsed.port || 443,
      path: parsed.pathname + parsed.search,
      method: 'GET',
      headers: Object.assign({
        'X-Requested-With': 'XMLHttpRequest'
      }, sigHeaders, extraHeaders || {})
    };
    const req = https.request(options, function (res) {
      const chunks = [];
      res.on('data', function (chunk) { chunks.push(chunk); });
      res.on('end', function () {
        const body = Buffer.concat(chunks).toString('utf-8');
        try { resolve({ status: res.statusCode, data: JSON.parse(body) }); }
        catch (_) { resolve({ status: res.statusCode, data: null, raw: body }); }
      });
    });
    req.on('error', function (e) { reject(e); });
    req.setTimeout(15000, function () { req.destroy(); reject(new Error('TIMEOUT')); });
    req.end();
  });
}

/**
 * qrcode — 通过服务端接口获取二维码图片 URL
 * 用法: node run.js qrcode <url>
 * 调用 https://click.meituan.com/cps/ai/product/getQrCodeImage
 */
commands.qrcode = function (argv) {
  const url = (argv || [])[0] || '';

  if (!url) {
    out({ ok: false, type: 'skip' });
    return;
  }

  // 支付链接不允许生成二维码，直接跳过
  if (url.indexOf('npay.meituan.com') !== -1) {
    out({ ok: false, type: 'skip', message: 'Payment URL is not allowed to generate QR code' });
    return;
  }

  const apiUrl = 'https://click.meituan.com/cps/ai/product/getQrCodeImage';
  const body = { originalUrl: url, clientSource: 'meituan-coupon' };

  requestQrCodeWithRetry(apiUrl, body, 1)
      .then(function (result) {
        if (result.imageUrl) {
          out({ ok: true, type: 'image', imageUrl: result.imageUrl });
        } else {
          out({ ok: false, type: 'skip', message: result.message, raw: result.raw });
        }
      });
};

/**
 * auth-prepare — 在一次命令内准备授权链接和二维码。
 * Token 缓存若在并发窗口中恢复，则直接执行 issue，不向模型输出 Token。
 */
commands['auth-prepare'] = function (argv) {
  const { args } = parseArgs(argv || []);
  const responseOutput = args.output === 'response';
  const authResult = getAuthCodeResult(argv);
  if (!authResult.ok) {
    if (responseOutput) {
      out(terminalResponse('登录链接生成失败，请稍后重试'));
      return;
    }
    const failure = {
      ok: false,
      success: false,
      error: authResult.error || 'AUTH_ERROR',
      message: '登录链接生成失败，请稍后重试'
    };
    if (authResult.code !== undefined) failure.code = authResult.code;
    out(failure);
    return;
  }
  if (authResult.type === 'token') {
    commands.issue(responseOutput ? ['--output', 'response'] : []);
    return;
  }
  if (authResult.type !== 'auth_link' || !authResult.url) {
    if (responseOutput) {
      out(terminalResponse('登录链接生成失败，请稍后重试'));
      return;
    }
    out({
      ok: false,
      success: false,
      error: 'AUTH_LINK_MISSING',
      message: '登录链接生成失败，请稍后重试'
    });
    return;
  }

  requestQrCodeWithRetry(
      'https://click.meituan.com/cps/ai/product/getQrCodeImage',
      { originalUrl: authResult.url, clientSource: 'meituan-coupon' },
      1
  ).then(function (qrResult) {
    const prepared = responseOutput
      ? { state: AUTH_REQUIRED_STATE, url: authResult.url }
      : { ok: true, type: 'auth_link', url: authResult.url };
    if (qrResult.imageUrl) prepared.imageUrl = qrResult.imageUrl;
    out(prepared);
  });
};

/**
 * auth-complete — 同步等待授权，成功后立即执行一次 issue。
 * response 模式下所有出口都返回最终正文。
 */
commands['auth-complete'] = function (argv) {
  const { args } = parseArgs(argv || []);
  const responseOutput = args.output === 'response';
  const pollResult = pollAuthTokenResult();
  if (!pollResult.ok) {
    out(responseOutput
      ? terminalResponse('登录失败，请重新登录')
      : pollResult);
    return;
  }
  commands.issue(responseOutput ? ['--output', 'response'] : []);
};

/**
 * execute — 完成初始化、设备标识、登录检查，并进入领券或授权分支。
 * response 模式只向模型返回 AUTH_REQUIRED 或带最终 response_text 的 TERMINAL。
 */
commands.execute = function (argv) {
  const { args } = parseArgs(argv || []);
  if (args.output !== 'response') {
    fail('OUTPUT_MODE_REQUIRED', { message: 'execute requires --output response' });
  }

  function runSelf(commandArgs, timeout) {
    const result = spawnSync(
        process.execPath,
        [...process.execArgv, __filename, ...commandArgs],
        {
          encoding: 'utf-8',
          timeout: timeout || 120000,
          stdio: ['ignore', 'pipe', 'pipe'],
          env: Object.assign({}, process.env, { NODE_OPTIONS: '' })
        }
    );
    const stdout = (result.stdout || '').trim();
    let payload = null;
    try { payload = JSON.parse(stdout); } catch (_) {}
    return {
      ok: result.status === 0 && !!payload,
      payload: payload
    };
  }

  function finishFailure(result) {
    const error = result && result.payload && result.payload.error;
    out(terminalResponse(setupResponseText(error || 'EXEC_ERROR')));
  }

  const initResult = runSelf(['init'], 120000);
  if (!initResult.ok || initResult.payload.ok !== true) {
    finishFailure(initResult);
    return;
  }

  const deviceResult = runSelf(['get-device-token'], 45000);
  if (!deviceResult.ok || deviceResult.payload.ok !== true) {
    finishFailure(deviceResult);
    return;
  }

  const tokenResult = runSelf(['get-token'], 630000);
  if (!tokenResult.ok) {
    finishFailure(tokenResult);
    return;
  }

  let nextResult = tokenResult.payload.ok === true
    ? runSelf(['issue', '--output', 'response', '--reauth-on-401'], 630000)
    : runSelf(['auth-prepare', '--output', 'response'], 630000);

  let payload = nextResult.payload;
  if (payload && payload.state === REAUTH_REQUIRED_STATE &&
      payload.reauth_required === true) {
    nextResult = runSelf(['auth-prepare', '--output', 'response'], 630000);
    payload = nextResult.payload;
  }
  const validAuth = payload && payload.state === AUTH_REQUIRED_STATE &&
      typeof payload.url === 'string' && payload.url.length > 0;
  const validTerminal = payload && payload.state === TERMINAL_STATE &&
      typeof payload.response_text === 'string' && payload.response_text.length > 0;
  if (!nextResult.ok || (!validAuth && !validTerminal)) {
    finishFailure(nextResult);
    return;
  }
  out(payload);
};

/**
 * issue — 领取优惠券（纯 Node.js 实现）
 * 用法: node run.js issue [--output response]
 *
 * Token 从 pt-passport 缓存自动读取，不通过命令行传递。
 *
 * 返回格式（成功）:
 *   { ok: true, success: true, coupon_count: N, coupons: [...], count_str: "...", display_coupons: [...] }
 * 返回格式（失败）:
 *   { ok: false, success: false, error: "<ERROR_TYPE>", message: "..." }
 */
commands.issue = function (argv) {
  const { args } = parseArgs(argv || []);
  const responseOutput = args.output === 'response';
  const reauthOn401 = args['reauth-on-401'] === 'true';
  const token = getCachedToken();

  const COUPON_BASE_URL = 'https://media.meituan.com';
  const COUPON_ISSUE_PATH = '/fulishemini/couponActivity/sendCouponTabbit';
  const CONFIG_FILE = path.join(SCRIPTS_DIR, 'config.json');
  const LOG_DIR = path.join(STATE_HOME, 'logs');
  const LOG_FILE = path.join(LOG_DIR, 'issue.log');

  // response 模式只返回已经渲染的最终正文；完整券数据仍保存在当天缓存中。
  function outIssueResult(result) {
    if (responseOutput) {
      if (reauthOn401 && result && result.code === 401) {
        out({ state: REAUTH_REQUIRED_STATE, reauth_required: true });
        return;
      }
      out(terminalResponse(issueResponseText(result)));
      return;
    }
    out(result);
  }

  if (!token) {
    if (responseOutput) {
      outIssueResult({
        ok: false,
        success: false,
        error: 'NO_TOKEN',
        message: '未登录或 Token 已过期，请先登录'
      });
      return;
    }
    fail('NO_TOKEN', { message: '未登录或 Token 已过期，请先登录' });
  }

  // 检查当天领券缓存，命中则返回 1014 + cached=true + 券数据，避免重复调发券接口
  var todayCache = loadTodayCouponCache();
  if (todayCache) {
    outIssueResult({
      ok: false, success: false, code: 1014, cached: true,
      message: '您今天已经领取过美团的优惠券',
      coupon_count: todayCache.coupon_count || 0,
      coupons: todayCache.coupons || [],
      count_str: todayCache.count_str || '',
      display_coupons: todayCache.display_coupons || [],
      activity_name: todayCache.activity_name || '',
      activity_link: todayCache.activity_link || ''
    });
    return;
  }

  // ── 工具函数 ──────────────────────────────────────────────
  function loadConfig() {
    try { return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf-8')); } catch (_) { return {}; }
  }

  // ── 当天领券缓存（只保留当天结果，跨天自动失效） ─────────
  function getLocalDateStr() {
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + day;
  }

  function loadTodayCouponCache() {
    try {
      const cacheFile = path.join(AUTH_DIR, 'today_coupons_cache.json');
      if (!fs.existsSync(cacheFile)) return null;
      const content = fs.readFileSync(cacheFile, 'utf-8');
      const cache = JSON.parse(content);
      const today = getLocalDateStr();
      if (cache && cache.date === today) return cache.data;
      return null;
    } catch (_) { return null; }
  }

  function saveTodayCouponCache(data) {
    try {
      const cacheFile = path.join(AUTH_DIR, 'today_coupons_cache.json');
      try { fs.mkdirSync(AUTH_DIR, { recursive: true }); } catch (_) {}
      const today = getLocalDateStr();
      const cache = { date: today, data: data };
      fs.writeFileSync(cacheFile, JSON.stringify(cache), 'utf-8');
      try { fs.chmodSync(cacheFile, 0o600); } catch (_) {}
    } catch (_) {}
  }

  function getDeviceToken() {
    try {
      const tokenFile = path.join(AUTH_DIR, 'token.json');
      return JSON.parse(fs.readFileSync(tokenFile, 'utf-8')).device_token || '';
    } catch (_) { return ''; }
  }

  function xorEncrypt(data, aiScene) {
    const deviceToken = getDeviceToken();
    var seed, flag;
    if (deviceToken) { seed = deviceToken + aiScene; flag = '1'; }
    else { seed = aiScene; flag = '0'; }
    const keyBytes = crypto.createHash('sha256').update(seed).digest();
    const dataBytes = Buffer.from(data, 'utf-8');
    const result = Buffer.alloc(dataBytes.length);
    for (var i = 0; i < dataBytes.length; i++) {
      result[i] = dataBytes[i] ^ keyBytes[i % 32];
    }
    return flag + ':' + result.toString('hex');
  }

  function writeLog(entry, aiScene) {
    try {
      try { fs.mkdirSync(LOG_DIR, { recursive: true }); } catch (_) {}
      var line = JSON.stringify(entry);
      fs.appendFileSync(LOG_FILE, line + '\n', 'utf-8');
      try { fs.chmodSync(LOG_FILE, 0o600); } catch (_) {}
    } catch (_) {}
  }

  function fenToYuan(fen) {
    if (!fen) return '0';
    var yuan = parseInt(fen, 10) / 100;
    return yuan === Math.floor(yuan) ? String(Math.floor(yuan)) : yuan.toFixed(1);
  }

  function formatTimestampMs(tsMs) {
    if (!tsMs) return '-';
    try {
      var d = new Date(parseInt(tsMs, 10));
      var year = d.getFullYear();
      var month = String(d.getMonth() + 1).padStart(2, '0');
      var day = String(d.getDate()).padStart(2, '0');
      return year + '-' + month + '-' + day;
    } catch (_) { return String(tsMs); }
  }

  function formatCoupon(c) {
    var priceLimit = c.priceLimit;
    var couponValue = c.couponValue || 0;
    var discountInfo = '';
    if (priceLimit && priceLimit > 0) {
      discountInfo = '满' + fenToYuan(priceLimit) + '元减' + fenToYuan(couponValue) + '元';
    }
    var start = c.couponStartTime;
    var end = c.couponEndTime;
    var validPeriod = '';
    if (start && end) {
      validPeriod = formatTimestampMs(start) + ' 至 ' + formatTimestampMs(end);
    }
    return {
      name: c.couponName || '',
      discount_info: discountInfo,
      valid_period: validPeriod,
      priceLimit: priceLimit,
      couponValue: couponValue,
      tabName: c.tabName || ''
    };
  }

  // ── 展示结果构建 ──────────────────────────────────────────
  var _TAB_ORDER = ['外卖', '美食团购', '美团闪购', '休闲娱乐', '生活服务', '丽人医疗', '更多福利'];
  var _TAB_DISPLAY = { '更多福利': '其他' };
  var _SLOT_PLAN_BASE = [['外卖', 2], ['美食团购', 1], ['美团闪购', 1],
                         ['休闲娱乐', 1], ['生活服务', 1], ['丽人医疗', 1]];

  function buildCountStr(coupons) {
    var tabCount = {};
    for (var i = 0; i < coupons.length; i++) {
      var tab = coupons[i].tabName || '';
      tabCount[tab] = (tabCount[tab] || 0) + 1;
    }
    var unknownTabs = Object.keys(tabCount).filter(function (t) { return _TAB_ORDER.indexOf(t) < 0; });
    var finalOrder = _TAB_ORDER.slice(0, 6).concat(unknownTabs, _TAB_ORDER.slice(6));
    var parts = [];
    for (var j = 0; j < finalOrder.length; j++) {
      var t = finalOrder[j];
      if (!tabCount[t]) continue;
      var displayName = _TAB_DISPLAY[t] || t;
      parts.push(displayName + '优惠券' + tabCount[t] + '张');
    }
    return parts.join('、');
  }

  function buildDisplayCoupons(coupons) {
    function sortKey(c) {
      var pl = c.priceLimit;
      if (!pl) return [0, 0];
      return [1, -(c.couponValue / pl)];
    }
    function compareFn(a, b) {
      var ka = sortKey(a), kb = sortKey(b);
      return ka[0] - kb[0] || ka[1] - kb[1];
    }
    var groups = {};
    for (var i = 0; i < coupons.length; i++) {
      var tab = coupons[i].tabName || '';
      if (!groups[tab]) groups[tab] = [];
      groups[tab].push(coupons[i]);
    }
    for (var t in groups) { groups[t].sort(compareFn); }

    var unknownTabs = Object.keys(groups).filter(function (t) { return _TAB_ORDER.indexOf(t) < 0; });
    var slotPlan = _SLOT_PLAN_BASE.concat(unknownTabs.map(function (t) { return [t, 1]; }), [['更多福利', 1]]);

    var used = {};
    var slots = [];

    for (var s = 0; s < slotPlan.length; s++) {
      if (slots.length >= 8) break;
      var slotTab = slotPlan[s][0], quota = slotPlan[s][1];
      var taken = 0;
      var grp = groups[slotTab] || [];
      for (var g = 0; g < grp.length; g++) {
        if (taken >= quota || slots.length >= 8) break;
        slots.push(grp[g]);
        used[slotTab] = (used[slotTab] || 0) + 1;
        taken++;
      }
    }

    var fallbackOrder = ['外卖', '美食团购', '美团闪购', '休闲娱乐', '生活服务', '丽人医疗']
        .concat(unknownTabs, ['更多福利']);
    while (slots.length < 8) {
      var filled = false;
      for (var f = 0; f < fallbackOrder.length; f++) {
        var remaining = (groups[fallbackOrder[f]] || []).slice(used[fallbackOrder[f]] || 0);
        if (remaining.length > 0) {
          slots.push(remaining[0]);
          used[fallbackOrder[f]] = (used[fallbackOrder[f]] || 0) + 1;
          filled = true;
          break;
        }
      }
      if (!filled) break;
    }
    return slots;
  }

  function buildDisplayResult(coupons) {
    return { count_str: buildCountStr(coupons), display_coupons: buildDisplayCoupons(coupons) };
  }

  // ── 发起请求 ──────────────────────────────────────────────
  var config = loadConfig();
  var aiScene = config.aiScene || '';

  var bodyObj = {
    token: token,
    aiScene: aiScene,
    version: 2
  };

  var logEntry = {
    time: new Date().toISOString().replace('T', ' ').slice(0, 19),
    request: {
      url: COUPON_BASE_URL + COUPON_ISSUE_PATH,
      aiScene: aiScene,
      token_present: Boolean(token)
    }
  };

  httpsPost(COUPON_BASE_URL + COUPON_ISSUE_PATH, bodyObj)
    .then(function (resp) {
      logEntry.response = { http_status: resp.status, body: JSON.stringify(resp.data).slice(0, 500) };

      var respData = resp.data;
      if (!respData) {
        logEntry.result = { success: false, error: 'PARSE_ERROR' };
        writeLog(logEntry, aiScene);
        outIssueResult({ ok: false, success: false, error: 'PARSE_ERROR', message: 'Invalid response from server' });
        return;
      }

      var code = respData.code;
      var msg = respData.msg || '';
      var data = respData.data || {};

      if (code === 200) {
        var couponList = data.couponList || [];
        var formattedCoupons = couponList.map(formatCoupon);
        var display = buildDisplayResult(formattedCoupons);
        var result = {
          ok: true, success: true, code: 200,
          coupon_count: formattedCoupons.length,
          coupons: formattedCoupons,
          count_str: display.count_str,
          display_coupons: display.display_coupons,
          activity_name: data.activityName || '',
          activity_link: data.activityLink || ''
        };
        saveTodayCouponCache(result);
        logEntry.result = { success: true, code: 200, coupon_count: formattedCoupons.length };
        writeLog(logEntry, aiScene);
        outIssueResult(result);
      } else if (code === 1014) {
        logEntry.result = { success: false, code: 1014, error: 'ALREADY_RECEIVED' };
        writeLog(logEntry, aiScene);
        outIssueResult({
          ok: false, success: false, code: 1014,
          error: 'ALREADY_RECEIVED',
          message: '您今天已经领取过了，每天只能领取一次，明天再来哦～',
          activity_name: data.activityName || '',
          activity_link: data.activityLink || ''
        });
      } else if (code === 401) {
        // 主动清除 pt-passport 本地缓存，防止 auth-get-code 命中旧 token 导致死循环
        try { runPassport(['logout', '--client_id', CLIENT_ID]); } catch (_) {}
        try { fs.unlinkSync(path.join(AUTH_DIR, 'pt_passport_auth.json')); } catch (_) {}

        logEntry.result = { success: false, code: 401, error: 'RE_LOGIN' };
        writeLog(logEntry, aiScene);
        outIssueResult({ ok: false, success: false, code: 401, error: 'RE_LOGIN', message: '登录已过期，请重新登录' });
      } else if (code === 509 || code === 50200) {
        logEntry.result = { success: false, code: code, error: 'RATE_LIMIT' };
        writeLog(logEntry, aiScene);
        outIssueResult({ ok: false, success: false, code: code, error: 'RATE_LIMIT', message: '请求过于频繁，请稍后重试' });
      } else if (code === 9999) {
        logEntry.result = { success: false, code: 9999, error: 'SYSTEM_ERROR' };
        writeLog(logEntry, aiScene);
        outIssueResult({ ok: false, success: false, code: 9999, error: 'SYSTEM_ERROR', message: '系统异常，请稍后重试' });
      } else {
        logEntry.result = { success: false, code: code, error: 'UNKNOWN_ERROR' };
        writeLog(logEntry, aiScene);
        outIssueResult({ ok: false, success: false, code: code, error: 'UNKNOWN_ERROR', message: '未知错误（code=' + code + '，msg=' + msg + '）' });
      }
    })
    .catch(function (e) {
      logEntry.response = { error: e.message === 'TIMEOUT' ? 'TIMEOUT' : 'NETWORK_ERROR', detail: e.message };
      writeLog(logEntry, aiScene);
      if (e.message === 'TIMEOUT') {
        outIssueResult({ ok: false, success: false, error: 'TIMEOUT', message: '请求超时，请稍后重试' });
      } else {
        outIssueResult({ ok: false, success: false, error: 'NETWORK_ERROR', message: '网络异常：' + e.message });
      }
    });
};

/**
* logout — 退出登录
* 直接通过 runPassport 清除 pt-passport CLI 缓存（避免 Python 找不到 pt-passport 的 PATH 问题）
*/
commands.logout = function () {
var cli_cleared = false;
try {
  var res = runPassport(['logout', '--client_id', CLIENT_ID]);
  cli_cleared = true;
} catch (e) {
  cli_cleared = false;
}
out({ ok: true, success: true, message: '已退出登录，下次需重新授权', device_token_preserved: true, cli_cache_cleared: cli_cleared });
};

/**
* clear-device-token — 清除设备标识
* 清除 device_token 并清除 pt-passport CLI 缓存
*/
commands['clear-device-token'] = function () {
// 先清除 pt-passport CLI 缓存
try { runPassport(['logout', '--client_id', CLIENT_ID]); } catch (e) {}
// 再清除本地 device_token（通过 auth.py）
var result = runPython('auth.py', ['clear-device-token']);
out(Object.assign({ ok: !!result.success }, result));
};

// ── 入口 ─────────────────────────────────────────────────────

const allArgs = process.argv.slice(2);
const command = allArgs[0];
const commandArgs = allArgs.slice(1);

if (!command || command === '--help' || command === '-h') {
  console.log(`Usage: node run.js <command> [options]

Commands:
  execute --output response       Initialize and enter claim/auth flow
  init                          Environment setup
  get-client-type               Detect client type (pc/mac/windows/miniprogram)
  get-device-token              Get device token
  get-token [--env test|prod]   Get cached user token
  auth-prepare [--env test|prod]  Prepare auth link and QR code
  auth-get-code [--env test|prod]  Get auth link
  auth-poll-token               Poll auth result
  auth-complete [--output response]  Poll auth and issue coupons
  qrcode <url>                  Get QR code image URL (server-side)
  issue [--output response]       Issue coupons
  logout                        Logout
  clear-device-token            Clear device token`);
  process.exit(0);
}

if (!commands[command]) {
  fail('UNKNOWN_COMMAND', { command, available: Object.keys(commands) });
}

commands[command](commandArgs);
