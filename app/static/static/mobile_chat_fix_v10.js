(() => {
  const mq = window.matchMedia('(max-width: 900px)');

  const getChatsView = () => document.querySelector('#chatsView');
  const getConversation = () => document.querySelector('#chatsView > .conversation');
  const getChatPanel = () => document.querySelector('#chatsView > .conversation .chat-panel');

  function isMobile() {
    return mq.matches;
  }

  function openMobileChat() {
    if (!isMobile()) return;

    const chatsView = getChatsView();
    const conversation = getConversation();
    if (!chatsView || !conversation) return;

    chatsView.classList.add('mobile-chat-mode');
    conversation.classList.add('mobile-chat-open');

    const panel = getChatPanel();
    if (panel) panel.classList.add('mobile-chat-open');

    document.documentElement.classList.add('mobile-chat-lock');
    document.body.classList.add('mobile-chat-lock');
  }

  function closeMobileChat() {
    const chatsView = getChatsView();
    const conversation = getConversation();

    if (chatsView) chatsView.classList.remove('mobile-chat-mode', 'mobile-chat-open');
    if (conversation) conversation.classList.remove('mobile-chat-open', 'active', 'open');

    document
      .querySelectorAll('#chatsView .chat-panel.mobile-chat-open')
      .forEach((el) => el.classList.remove('mobile-chat-open'));

    document
      .querySelectorAll('#chatsView .chat-item.active, #chatsView .chat-item.selected, #chatsView .chat-item.is-active')
      .forEach((el) => {
        el.classList.remove('active', 'selected', 'is-active');
        el.removeAttribute('aria-selected');
      });

    document.documentElement.classList.remove('mobile-chat-lock');
    document.body.classList.remove('mobile-chat-lock');
  }

  document.addEventListener('click', (event) => {
    const backButton = event.target.closest('#chatsView .mobile-back-btn');
    if (backButton && isMobile()) {
      closeMobileChat();
      return;
    }

    const chatItem = event.target.closest('#chatsView .chat-item');
    if (chatItem && isMobile()) {
      openMobileChat();

      // Re-apply after the app finishes rendering the selected conversation.
      requestAnimationFrame(openMobileChat);
      setTimeout(openMobileChat, 80);
      setTimeout(openMobileChat, 240);
    }
  }, true);

  const observer = new MutationObserver(() => {
    const chatsView = getChatsView();
    if (!isMobile() || !chatsView || !chatsView.classList.contains('mobile-chat-mode')) return;
    openMobileChat();
  });

  observer.observe(document.documentElement, {
    childList: true,
    subtree: true
  });

  mq.addEventListener?.('change', () => {
    if (!isMobile()) closeMobileChat();
  });

  window.ArtiMobileChatFix = {
    open: openMobileChat,
    close: closeMobileChat
  };
})();
