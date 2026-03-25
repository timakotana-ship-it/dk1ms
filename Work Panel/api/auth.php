<?php
header('Content-Type: application/json');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: POST, GET');
header('Access-Control-Allow-Headers: Content-Type');

$data_dir = __DIR__ . '/../data/';
$pending_file = $data_dir . 'pending_codes.json';

if (!file_exists($data_dir)) mkdir($data_dir, 0777, true);

$input = json_decode(file_get_contents('php://input'), true);
$action = $input['action'] ?? $_GET['action'] ?? '';

if ($action === 'generate') {
    $codes = file_exists($pending_file) ? json_decode(file_get_contents($pending_file), true) : [];
    $code = substr(str_shuffle('ABCDEFGHJKLMNPQRSTUVWXYZ0123456789'), 0, 6);
    $session = bin2hex(random_bytes(16));
    $codes[$code] = ['session' => $session, 'expires' => time() + 300];
    file_put_contents($pending_file, json_encode($codes));
    echo json_encode(['code' => $code]);
    exit;
}

if ($action === 'check') {
    $code = $input['code'] ?? $_GET['code'] ?? '';
    $codes = file_exists($pending_file) ? json_decode(file_get_contents($pending_file), true) : [];
    if (isset($codes[$code])) {
        $users = json_decode(file_get_contents($data_dir . 'users.json'), true) ?: [];
        foreach ($users as $user) {
            if ($user['session'] === $codes[$code]['session'] && isset($user['tg_id'])) {
                session_start();
                $_SESSION['user_id'] = $user['session'];
                echo json_encode(['authenticated' => true, 'session' => $user['session']]);
                exit;
            }
        }
    }
    echo json_encode(['authenticated' => false]);
    exit;
}