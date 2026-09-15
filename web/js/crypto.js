/* EasyEssay · 浏览器端密钥加密（WebCrypto）

为什么用加密而不是哈希：服务器必须拿到**明文**密钥才能去调 DeepSeek，
而哈希不可逆、拿到哈希也调不了接口。所以密钥只能加密，关键在"谁持有解密钥"。

本项目的做法：
  - 用**用户自己的登录密码** + 随机盐，经 PBKDF2-SHA256（20 万次）派生 AES-GCM 密钥；
  - 加密只发生在浏览器，服务器只收到 {salt, iv, ct} 密文；
  - 服务器没有密码、也没有派生密钥 → **解不开**，站长拿到 users.json 也只有密文；
  - 改密码时用旧密钥解密、新密钥重新加密，服务器全程不参与解密。

注意：`crypto.subtle` 只在安全上下文可用（https 或 localhost）。
如果朋友通过 http://192.168.x.x 访问，浏览器不给用 —— 此时只能用「本次会话」模式。
*/
(function (global) {
  'use strict';
  var EE = global.EasyEssay = global.EasyEssay || {};

  var PBKDF2_ITER = 200000;

  function subtle() {
    return (global.crypto && global.crypto.subtle) || null;
  }

  function available() {
    return !!subtle();
  }

  function b64(buf) {
    var bytes = new Uint8Array(buf);
    var s = '';
    for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  }

  function unb64(text) {
    var s = atob(text);
    var out = new Uint8Array(s.length);
    for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
    return out;
  }

  function deriveKey(password, saltB64, iterations) {
    var st = subtle();
    if (!st) return Promise.reject(new Error('当前环境不支持浏览器加密（需要 https 或 localhost）'));
    var enc = new TextEncoder();
    return st.importKey('raw', enc.encode(password), 'PBKDF2', false, ['deriveKey'])
      .then(function (base) {
        return st.deriveKey(
          { name: 'PBKDF2', salt: unb64(saltB64), iterations: iterations || PBKDF2_ITER, hash: 'SHA-256' },
          base, { name: 'AES-GCM', length: 256 }, false, ['encrypt', 'decrypt']);
      });
  }

  /** 明文 -> 可直接交给服务器的密文对象 */
  function encrypt(plain, password, iterations) {
    var st = subtle();
    if (!st) return Promise.reject(new Error('当前环境不支持浏览器加密'));
    var salt = global.crypto.getRandomValues(new Uint8Array(16));
    var iv = global.crypto.getRandomValues(new Uint8Array(12));
    var iter = iterations || PBKDF2_ITER;
    return deriveKey(password, b64(salt), iter).then(function (key) {
      return st.encrypt({ name: 'AES-GCM', iv: iv }, key, new TextEncoder().encode(plain))
        .then(function (ct) {
          return {
            v: 1, kdf: 'PBKDF2-SHA256', iter: iter,
            salt: b64(salt), iv: b64(iv), ct: b64(ct)
          };
        });
    });
  }

  /** 服务器存的密文 -> 明文（密码不对会抛错） */
  function decrypt(blob, password) {
    var st = subtle();
    if (!st) return Promise.reject(new Error('当前环境不支持浏览器加密'));
    if (!blob || !blob.ct) return Promise.resolve('');
    return deriveKey(password, blob.salt, blob.iter).then(function (key) {
      return st.decrypt({ name: 'AES-GCM', iv: unb64(blob.iv) }, key, unb64(blob.ct))
        .then(function (buf) { return new TextDecoder().decode(buf); });
    });
  }

  EE.crypto = {
    available: available,
    encrypt: encrypt,
    decrypt: decrypt,
    PBKDF2_ITER: PBKDF2_ITER
  };
})(window);
