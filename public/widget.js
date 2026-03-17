/**
 * ClinicAI — Embeddable Chat Widget
 * 
 * Usage: Add this script to any website with a data-clinic-id attribute:
 *   <script src="https://your-domain.com/widget.js" data-clinic-id="your-clinic-id"></script>
 * 
 * Optional attributes:
 *   data-api-url    — Override the default API endpoint
 *   data-position   — "right" (default) or "left"
 *   data-color      — Primary color hex (e.g., "#0891b2")
 */
(function () {
  'use strict';

  // ── Read config from script tag ──
  const scriptTag = document.currentScript;
  const CLINIC_ID = scriptTag?.getAttribute('data-clinic-id') || 'default';
  const API_URL = scriptTag?.getAttribute('data-api-url') || 'https://mczhyqnehkmzyijsktcf.supabase.co/functions/v1/chat';
  const POSITION = scriptTag?.getAttribute('data-position') || 'right';
  const PRIMARY = scriptTag?.getAttribute('data-color') || '#0891b2';

  // ── State ──
  let conversationId = null;
  let isSending = false;
  let chatOpened = false;
  let greetingSent = false;
  let typingEl = null;

  // ── TTS ──
  function speakText(text) {
    if (!window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.05; u.pitch = 1.0;
    const voices = window.speechSynthesis.getVoices();
    const v = voices.find(v => v.name.includes('Samantha')) ||
              voices.find(v => v.lang === 'en-US' && v.name.includes('Female')) ||
              voices.find(v => v.lang === 'en-US');
    if (v) u.voice = v;
    window.speechSynthesis.speak(u);
  }
  if (window.speechSynthesis) {
    window.speechSynthesis.getVoices();
    window.speechSynthesis.onvoiceschanged = () => window.speechSynthesis.getVoices();
  }

  // ── Inject Styles ──
  const style = document.createElement('style');
  style.textContent = `
    #clinicai-btn {
      position: fixed; bottom: 24px; ${POSITION}: 24px;
      z-index: 2147483646;
      display: flex; align-items: center; gap: 8px;
      padding: 14px 24px;
      background: linear-gradient(135deg, ${PRIMARY}, ${PRIMARY}dd);
      color: white; border: none; border-radius: 9999px;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      font-size: 14px; font-weight: 600; cursor: pointer;
      box-shadow: 0 6px 24px ${PRIMARY}4d;
      transition: transform 0.3s, box-shadow 0.3s, width 0.3s, padding 0.3s, border-radius 0.3s;
    }
    #clinicai-btn:hover { transform: translateY(-2px); box-shadow: 0 10px 32px ${PRIMARY}59; }
    #clinicai-btn .cai-icon { width: 18px; height: 18px; fill: white; flex-shrink: 0; }
    #clinicai-btn .cai-label { white-space: nowrap; }
    #clinicai-btn .cai-close { display: none; width: 22px; height: 22px; fill: white; }
    #clinicai-btn.active .cai-icon, #clinicai-btn.active .cai-label { display: none; }
    #clinicai-btn.active .cai-close { display: block; }
    #clinicai-btn.active { padding: 16px; border-radius: 50%; width: 54px; height: 54px; justify-content: center; }

    #clinicai-widget {
      position: fixed; bottom: 96px; ${POSITION}: 24px;
      width: 380px; height: 540px;
      background: #fff; border: 1px solid #e2e8f0;
      border-radius: 20px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.12);
      display: none; flex-direction: column;
      overflow: hidden; z-index: 2147483647;
      opacity: 0; transform: translateY(16px) scale(0.96);
      transition: opacity 0.4s cubic-bezier(0.4, 0, 0.2, 1), transform 0.4s cubic-bezier(0.4, 0, 0.2, 1);
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    #clinicai-widget.open { display: flex; }
    #clinicai-widget.visible { opacity: 1; transform: translateY(0) scale(1); }

    #clinicai-header {
      background: linear-gradient(135deg, ${PRIMARY}, ${PRIMARY}dd);
      padding: 16px 20px; display: flex; align-items: center; color: white;
    }
    .clinicai-hdr-info { display: flex; align-items: center; gap: 10px; }
    .clinicai-hdr-avatar {
      width: 36px; height: 36px; background: rgba(255,255,255,0.2);
      border-radius: 10px; display: flex; align-items: center; justify-content: center;
    }
    .clinicai-hdr-avatar svg { width: 18px; height: 18px; fill: white; }
    .clinicai-hdr-name { font-weight: 600; font-size: 14px; }
    .clinicai-hdr-sub { font-size: 11px; color: rgba(255,255,255,0.7); }

    #clinicai-chat {
      flex: 1; padding: 16px 18px; overflow-y: auto;
      display: flex; flex-direction: column; gap: 8px;
      scroll-behavior: smooth; background: #fafafa;
    }
    #clinicai-chat::-webkit-scrollbar { width: 3px; }
    #clinicai-chat::-webkit-scrollbar-thumb { background: #ddd; border-radius: 2px; }

    .cai-bubble {
      max-width: 82%; padding: 11px 15px; border-radius: 16px;
      font-size: 13px; line-height: 1.5; word-wrap: break-word;
      animation: caiMsgIn 0.3s ease;
    }
    @keyframes caiMsgIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
    .cai-bubble-ai {
      background: white; color: #1e293b; align-self: flex-start;
      border-bottom-left-radius: 4px; border: 1px solid #e2e8f0;
    }
    .cai-bubble-user {
      background: linear-gradient(135deg, ${PRIMARY}, ${PRIMARY}dd);
      color: white; align-self: flex-end; border-bottom-right-radius: 4px;
    }

    .cai-typing { display: flex; align-items: center; gap: 4px; padding: 12px 16px; align-self: flex-start; }
    .cai-typing span {
      width: 6px; height: 6px; background: #94a3b8; border-radius: 50%;
      animation: caiBounce 1.4s infinite ease-in-out;
    }
    .cai-typing span:nth-child(1) { animation-delay: 0s; }
    .cai-typing span:nth-child(2) { animation-delay: 0.2s; }
    .cai-typing span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes caiBounce { 0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; } 40% { transform: scale(1); opacity: 1; } }

    #clinicai-controls {
      padding: 14px 18px; border-top: 1px solid #e2e8f0;
      display: flex; gap: 8px; background: white;
    }
    #clinicai-input {
      flex: 1; padding: 10px 14px; background: #f1f5f9;
      border: 1px solid #e2e8f0; border-radius: 9999px;
      outline: none; font-size: 13px; color: #1e293b;
      font-family: inherit; transition: border-color 0.3s;
    }
    #clinicai-input::placeholder { color: #94a3b8; }
    #clinicai-input:focus { border-color: ${PRIMARY}; }
    #clinicai-send {
      background: ${PRIMARY}; color: white; border: none; cursor: pointer;
      border-radius: 50%; width: 36px; height: 36px;
      display: flex; align-items: center; justify-content: center;
      flex-shrink: 0; transition: transform 0.2s;
    }
    #clinicai-send:hover { filter: brightness(0.9); }
    #clinicai-send:active { transform: scale(0.95); }
    #clinicai-send svg { width: 15px; height: 15px; fill: white; margin-left: 2px; }

    @media (max-width: 480px) {
      #clinicai-widget { ${POSITION}: 10px; left: 10px; right: 10px; width: auto; bottom: 88px; height: 70vh; }
    }
  `;
  document.head.appendChild(style);

  // ── Inject HTML ──
  const container = document.createElement('div');
  container.innerHTML = `
    <button id="clinicai-btn">
      <svg class="cai-icon" viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H6l-2 2V4h16v12z"/></svg>
      <span class="cai-label">Chat with us</span>
      <svg class="cai-close" viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
    </button>
    <div id="clinicai-widget">
      <div id="clinicai-header">
        <div class="clinicai-hdr-info">
          <div class="clinicai-hdr-avatar">
            <svg viewBox="0 0 24 24"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/></svg>
          </div>
          <div>
            <div class="clinicai-hdr-name">AI Assistant</div>
            <div class="clinicai-hdr-sub">Powered by ClinicAI</div>
          </div>
        </div>
      </div>
      <div id="clinicai-chat"></div>
      <div id="clinicai-controls">
        <input type="text" id="clinicai-input" placeholder="Type a message...">
        <button id="clinicai-send" title="Send">
          <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
        </button>
      </div>
    </div>
  `;
  document.body.appendChild(container);

  // ── DOM References ──
  const btn = document.getElementById('clinicai-btn');
  const widget = document.getElementById('clinicai-widget');
  const chatBox = document.getElementById('clinicai-chat');
  const input = document.getElementById('clinicai-input');
  const sendBtn = document.getElementById('clinicai-send');

  // ── Toggle ──
  btn.addEventListener('click', () => {
    if (widget.classList.contains('open')) {
      widget.classList.remove('visible');
      setTimeout(() => widget.classList.remove('open'), 400);
      btn.classList.remove('active');
    } else {
      widget.classList.add('open');
      requestAnimationFrame(() => requestAnimationFrame(() => widget.classList.add('visible')));
      btn.classList.add('active');
      chatOpened = true;
      if (!greetingSent) { greetingSent = true; sendMessage('hello'); }
    }
  });

  // ── Helpers ──
  function scrollToBottom() { chatBox.scrollTop = chatBox.scrollHeight; }

  function addBubble(text, sender) {
    removeTyping();
    const div = document.createElement('div');
    div.className = 'cai-bubble cai-bubble-' + sender;
    div.textContent = text;
    chatBox.appendChild(div);
    scrollToBottom();
  }

  function showTyping() {
    if (typingEl) return;
    typingEl = document.createElement('div');
    typingEl.className = 'cai-typing';
    typingEl.innerHTML = '<span></span><span></span><span></span>';
    chatBox.appendChild(typingEl);
    scrollToBottom();
  }

  function removeTyping() {
    if (typingEl) { typingEl.remove(); typingEl = null; }
  }

  // ── API ──
  async function sendMessage(text) {
    if (!text.trim() || isSending) return;
    isSending = true;

    // Don't show user bubble for initial greeting
    if (text.trim().toLowerCase() !== 'hello' || chatBox.children.length > 0) {
      addBubble(text, 'user');
    }
    showTyping();

    try {
      const resp = await fetch(API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: text,
          conversation_id: conversationId,
          clinic_id: CLINIC_ID,
        }),
      });
      const data = await resp.json();
      removeTyping();

      if (data.error) {
        addBubble('Sorry, something went wrong. Please try again.', 'ai');
      } else {
        conversationId = data.conversation_id;
        addBubble(data.response, 'ai');
        speakText(data.response);
      }
    } catch (e) {
      removeTyping();
      addBubble('Connection error. Please try again.', 'ai');
      console.error('[ClinicAI] Error:', e);
    }
    isSending = false;
  }

  // ── Events ──
  input.addEventListener('keypress', (e) => {
    if (e.key === 'Enter') { const v = input.value; input.value = ''; sendMessage(v); }
  });
  sendBtn.addEventListener('click', () => { const v = input.value; input.value = ''; sendMessage(v); });

})();
