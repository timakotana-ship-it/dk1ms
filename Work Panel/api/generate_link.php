<?php
session_start();
header('Content-Type: application/json');
$data_dir = __DIR__ . '/../data/';
if (!isset($_SESSION['user_id'])) {
    http_response_code(401);
    echo json_encode(['error' => 'Unauthorized']);
    exit;
}
$user_session = $_SESSION['user_id'];
$users = json_decode(file_get_contents($data_dir . 'users.json'), true);
$user = null;
foreach ($users as $u) {
    if ($u['session'] === $user_session) {
        $user = $u;
        break;
    }
}
if (!$user) {
    http_response_code(401);
    echo json_encode(['error' => 'User not found']);
    exit;
}
// Проверка реквизитов
if (empty($user['wallets']['trc20']) && empty($user['wallets']['erc20']) && 
    empty($user['wallets']['bep20']) && empty($user['wallets']['ton'])) {
    http_response_code(400);
    echo json_encode(['error' => 'Заполните реквизиты']);
    exit;
}
$code = bin2hex(random_bytes(8));
$links = file_exists($data_dir . 'links.json') ? json_decode(file_get_contents($data_dir . 'links.json'), true) : [];
$links[] = [
    'code' => $code,
    'user_session' => $user_session,
    'url' => "http://127.0.0.1:8080/{$code}",
    'created' => date('Y-m-d H:i:s'),
    'clicks' => 0,
    'success' => 0
];
file_put_contents($data_dir . 'links.json', json_encode($links, JSON_PRETTY_PRINT));
echo json_encode(['url' => "http://127.0.0.1:8080/{$code}"]);