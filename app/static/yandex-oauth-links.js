// Administrator form for associating a Yandex login or email with a CRM employee.
const yandexOAuthLinks = (() => {
  let loadVersion = 0;

  function reset() {
    loadVersion += 1;
    const list = $('yandexOAuthLinksList');
    const userSelect = $('yandexOAuthCrmUser');
    const identifierInput = $('yandexOAuthIdentifier');
    const identifierType = $('yandexOAuthIdentifierType');
    if (list) list.innerHTML = '';
    if (userSelect) userSelect.innerHTML = '';
    if (identifierInput) identifierInput.value = '';
    if (identifierType) identifierType.value = 'login';
  }

  function render(links, users) {
    const userSelect = $('yandexOAuthCrmUser');
    if (userSelect) {
      userSelect.innerHTML = users.map((user) => {
        const title = user.display_name || user.username;
        const inactive = user.is_active ? '' : ' · отключён';
        return `<option value="${Number(user.id)}">${escapeHtml(title)} (@${escapeHtml(user.username)}${inactive})</option>`;
      }).join('');
    }

    const list = $('yandexOAuthLinksList');
    if (!list) return;
    if (!links.length) {
      list.innerHTML = '<p class="muted">Связей пока нет.</p>';
      return;
    }
    list.innerHTML = links.map((link) => {
      const identityLabel = link.identifier_type === 'email' ? 'Email' : 'Логин';
      const crmLabel = link.crm_display_name || link.crm_username || `CRM #${Number(link.crm_user_id)}`;
      const activeLabel = link.is_active ? 'Включена' : 'Отключена';
      const confirmedLabel = link.confirmed ? 'Подтверждена' : 'Ожидает первого входа';
      return `
        <article class="user-row user-row-compact" data-yandex-oauth-link-id="${Number(link.id)}">
          <div class="user-summary">
            <strong>${escapeHtml(identityLabel)}: ${escapeHtml(link.identifier)}</strong>
            <p>${escapeHtml(crmLabel)} · ${escapeHtml(confirmedLabel)}</p>
            <div class="user-chip-row">
              <span class="user-status-chip ${link.is_active ? 'is-active' : 'is-disabled'}">${escapeHtml(activeLabel)}</span>
            </div>
          </div>
          <div class="user-create-form-actions">
            <button class="secondary" type="button" data-yandex-oauth-toggle="${Number(link.id)}" data-active="${link.is_active ? '1' : '0'}">
              ${link.is_active ? 'Отключить' : 'Включить'}
            </button>
            <button class="secondary" type="button" data-yandex-oauth-delete="${Number(link.id)}">Запретить вход</button>
          </div>
        </article>
      `;
    }).join('');
  }

  async function load() {
    if (!currentUser || currentUser.role !== 'admin') return;
    const requestId = ++loadVersion;
    const payload = await api('/api/admin/yandex-oauth-links');
    if (requestId !== loadVersion || currentUser?.role !== 'admin') return;
    const links = Array.isArray(payload?.links) ? payload.links : [];
    const users = Array.isArray(payload?.users) ? payload.users : [];
    render(links, users);
  }

  async function submit(event) {
    event.preventDefault();
    const identifier = $('yandexOAuthIdentifier')?.value?.trim() || '';
    const crmUserId = Number($('yandexOAuthCrmUser')?.value || 0);
    if (!identifier || !crmUserId) {
      notify('Проверьте данные', 'Укажите логин или email и сотрудника CRM.');
      return;
    }
    try {
      await api('/api/admin/yandex-oauth-links', {
        method: 'POST',
        body: JSON.stringify({
          identifier_type: $('yandexOAuthIdentifierType')?.value || 'login',
          identifier,
          crm_user_id: crmUserId,
        }),
      });
      if ($('yandexOAuthIdentifier')) $('yandexOAuthIdentifier').value = '';
      await load();
      notify('Готово', 'Связь для входа через Яндекс добавлена.');
    } catch (err) {
      notify('Не удалось добавить связь', String(err.message || err));
    }
  }

  async function handleClick(event) {
    const toggleButton = event.target.closest?.('[data-yandex-oauth-toggle]');
    const deleteButton = event.target.closest?.('[data-yandex-oauth-delete]');
    try {
      if (toggleButton) {
        event.preventDefault();
        const linkId = Number(toggleButton.dataset.yandexOauthToggle || 0);
        const isActive = toggleButton.dataset.active === '1';
        await api(`/api/admin/yandex-oauth-links/${linkId}`, {
          method: 'PATCH',
          body: JSON.stringify({ is_active: !isActive }),
        });
        await load();
        return;
      }
      if (deleteButton) {
        event.preventDefault();
        const linkId = Number(deleteButton.dataset.yandexOauthDelete || 0);
        if (!window.confirm('Запретить новые входы через Яндекс? Открытые CRM-сессии останутся активными до выхода или истечения срока.')) return;
        await api(`/api/admin/yandex-oauth-links/${linkId}`, { method: 'DELETE' });
        await load();
      }
    } catch (err) {
      notify('Не удалось изменить связь', String(err.message || err));
    }
  }

  function bind() {
    const yandexOAuthLinkForm = $('yandexOAuthLinkForm');
    if (yandexOAuthLinkForm && !yandexOAuthLinkForm.dataset.bound) {
      yandexOAuthLinkForm.dataset.bound = '1';
      yandexOAuthLinkForm.addEventListener('submit', submit);
    }
    const yandexOAuthLinksList = $('yandexOAuthLinksList');
    if (yandexOAuthLinksList && !yandexOAuthLinksList.dataset.bound) {
      yandexOAuthLinksList.dataset.bound = '1';
      yandexOAuthLinksList.addEventListener('click', handleClick);
    }
  }

  return { bind, load, reset };
})();
