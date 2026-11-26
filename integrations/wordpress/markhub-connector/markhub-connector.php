<?php
/**
 * Plugin Name:       MarkHub Connector
 * Description:       Sends www.solix.com form submissions (request a demo, contact sales, gated downloads) to MarkHub, server to server, so every lead lands in the MarkHub inbox, gets scored and is linked to the visitor's browsing.
 * Version:           1.0.0
 * Requires at least: 6.0
 * Requires PHP:      7.4
 * Author:            Solix Technologies
 * License:           Proprietary
 *
 * How it works
 * - Runs after WordPress (the theme's own form handler) has processed a POST, so the site's existing emails and
 *   CRM hand-offs keep working unchanged. Only POSTs whose fields match a known form are forwarded; anything else
 *   (logins, comments, search) is ignored.
 * - The MarkHub tag (loaded through Google Tag Manager) adds a hidden "markhub_visitor_id" field to forms once the
 *   visitor has accepted analytics cookies; it is passed on so MarkHub can link the lead to their browsing.
 * - If MarkHub can't be reached, the lead is queued and retried every five minutes (wp-cron), so a slow or sleeping
 *   API never loses one. Settings > MarkHub shows the last forwards and lets you test the connection.
 * - Other forms can be added with the "markhub_connector_forms" filter (see markhub_connector_forms()).
 */

if (!defined('ABSPATH')) {
    exit;
}

const MARKHUB_OPT = 'markhub_connector';
const MARKHUB_QUEUE = 'markhub_connector_queue';
const MARKHUB_LOG = 'markhub_connector_log';
const MARKHUB_CRON = 'markhub_connector_retry';
const MARKHUB_QUEUE_MAX = 500;
const MARKHUB_LOG_MAX = 25;

/** Settings: the API address and the shared key (WP_INTAKE_SECRET on the MarkHub side). wp-config.php constants win. */
function markhub_connector_settings()
{
    $o = get_option(MARKHUB_OPT, array());
    return array(
        'api' => rtrim(defined('MARKHUB_API_URL') ? MARKHUB_API_URL : (isset($o['api']) ? $o['api'] : 'https://solix-backend-mf5w.onrender.com'), '/'),
        'key' => defined('MARKHUB_INTAKE_KEY') ? MARKHUB_INTAKE_KEY : (isset($o['key']) ? $o['key'] : ''),
        'enabled' => defined('MARKHUB_ENABLED') ? (bool) MARKHUB_ENABLED : !empty($o['enabled']),
    );
}

/**
 * Known forms on www.solix.com. Each entry: the field that identifies it ("detect"), the MarkHub submission type, and
 * a map of MarkHub field => WordPress field name (or a list of names, joined with a space, e.g. first + last name).
 */
function markhub_connector_forms()
{
    $forms = array(
        'request-demo' => array(
            'detect' => 'demo_email',
            'type' => 'demo',
            'map' => array(
                'email' => 'demo_email',
                'name' => array('demo_fName', 'demo_lName'),
                'company' => 'demo_business_name',
                'job_title' => 'demo_jobtitle',
                'message' => 'reg_message',
                'interest' => 'reg_solution_name',
            ),
        ),
        'contact-sales' => array(
            'detect' => 'cts_email',
            'type' => 'contact',
            'map' => array(
                'email' => 'cts_email',
                'name' => array('cts_first_name', 'cts_last_name'),
                'company' => 'cts_company_name',
                'phone' => 'cts_phone',
                'interest' => array('cts_interest', 'cts_enquiry'),
                'message' => 'cts_message',
            ),
        ),
        'contact-question' => array(
            'detect' => 'full_name',
            'requires' => array('email', 'msg'),
            'type' => 'contact',
            'map' => array(
                'email' => 'email',
                'name' => 'full_name',
                'company' => 'company',
                'phone' => 'phone',
                'message' => 'msg',
            ),
        ),
        'gated-download' => array(
            'detect' => 'new_email',
            'type' => 'download',
            'map' => array(
                'email' => 'new_email',
                'name' => array('first_name', 'last_name'),
                'company' => 'company_name',
                'phone' => 'user_phone',
                'country' => 'user_country',
            ),
        ),
    );
    return apply_filters('markhub_connector_forms', $forms);
}

/** Which known form this POST is, or null. */
function markhub_connector_match(array $post)
{
    foreach (markhub_connector_forms() as $id => $form) {
        if (empty($post[$form['detect']])) {
            continue;
        }
        if (!empty($form['requires'])) {
            foreach ($form['requires'] as $f) {
                if (empty($post[$f])) {
                    continue 2;
                }
            }
        }
        return $id;
    }
    return null;
}

function markhub_connector_field(array $post, $names, $max = 500)
{
    $parts = array();
    foreach ((array) $names as $n) {
        if (isset($post[$n]) && is_scalar($post[$n])) {
            $v = trim(sanitize_text_field(wp_unslash((string) $post[$n])));
            if ($v !== '') {
                $parts[] = $v;
            }
        }
    }
    $v = implode(' ', $parts);
    return $v === '' ? null : mb_substr($v, 0, $max);
}

/** The MarkHub payload for this POST, or null when it isn't a lead form we know. */
function markhub_connector_payload(array $post, $form_id)
{
    $forms = markhub_connector_forms();
    $form = $forms[$form_id];
    $limits = array('message' => 5000, 'country' => 80, 'interest' => 300);
    $p = array('type' => $form['type'], 'form' => $form_id);
    foreach ($form['map'] as $to => $from) {
        $v = markhub_connector_field($post, $from, isset($limits[$to]) ? $limits[$to] : 300);
        if ($v !== null) {
            $p[$to] = $v;
        }
    }
    if (empty($p['email']) || !is_email($p['email'])) {
        return null;
    }
    $scheme = is_ssl() ? 'https://' : 'http://';
    $host = isset($_SERVER['HTTP_HOST']) ? sanitize_text_field(wp_unslash($_SERVER['HTTP_HOST'])) : parse_url(home_url(), PHP_URL_HOST);
    $uri = isset($_SERVER['REQUEST_URI']) ? esc_url_raw(wp_unslash($_SERVER['REQUEST_URI'])) : '/';
    // Forms posted through admin-ajax.php: the page they were on is the referer.
    $page = (strpos($uri, 'admin-ajax.php') !== false && wp_get_referer()) ? wp_get_referer() : $scheme . $host . $uri;
    $p['source_page'] = mb_substr(strtok($page, '?'), 0, 300);
    if ($form['type'] === 'download') {
        $path = trim((string) parse_url($p['source_page'], PHP_URL_PATH), '/');
        $slug = $path === '' ? '' : basename($path);
        $p['resource_slug'] = $slug !== '' ? mb_substr(sanitize_title($slug), 0, 120) : null;
        $title = $slug !== '' ? ucwords(str_replace('-', ' ', $slug)) : '';
        if ($title !== '') {
            $p['resource'] = mb_substr($title, 0, 300);
        }
    }
    $vid = markhub_connector_field($post, 'markhub_visitor_id', 64);
    if ($vid && preg_match('/^[A-Za-z0-9_-]{8,64}$/', $vid)) {
        $p['visitor_id'] = $vid;
    }
    $ip = isset($_SERVER['REMOTE_ADDR']) ? sanitize_text_field(wp_unslash($_SERVER['REMOTE_ADDR'])) : '';
    if ($ip !== '') {
        $p['client_ip'] = mb_substr($ip, 0, 64);
    }
    return apply_filters('markhub_connector_payload', array_filter($p, function ($v) { return $v !== null && $v !== ''; }), $form_id, $post);
}

/** POST one lead to MarkHub. Returns true on success, or an error string. */
function markhub_connector_send(array $payload, $timeout = 8)
{
    $s = markhub_connector_settings();
    if ($s['api'] === '' || $s['key'] === '') {
        return 'not configured';
    }
    $r = wp_remote_post($s['api'] . '/api/integrations/wordpress/submissions', array(
        'timeout' => $timeout,
        'headers' => array('Content-Type' => 'application/json', 'X-MarkHub-Key' => $s['key']),
        'body' => wp_json_encode($payload),
        'data_format' => 'body',
    ));
    if (is_wp_error($r)) {
        return $r->get_error_message();
    }
    $code = (int) wp_remote_retrieve_response_code($r);
    if ($code >= 200 && $code < 300) {
        return true;
    }
    return 'HTTP ' . $code . ': ' . mb_substr(wp_remote_retrieve_body($r), 0, 200);
}

function markhub_connector_log($form, $email, $result)
{
    $log = get_option(MARKHUB_LOG, array());
    array_unshift($log, array(
        'at' => gmdate('Y-m-d H:i:s'),
        'form' => $form,
        // Only the domain: this log is visible to any admin and isn't the place to keep people's addresses.
        'who' => strpos($email, '@') !== false ? '…@' . substr(strrchr($email, '@'), 1) : '',
        'result' => $result === true ? 'sent' : $result,
    ));
    update_option(MARKHUB_LOG, array_slice($log, 0, MARKHUB_LOG_MAX), false);
}

function markhub_connector_enqueue(array $payload)
{
    $q = get_option(MARKHUB_QUEUE, array());
    $q[] = array('payload' => $payload, 'tries' => 0, 'since' => time());
    if (count($q) > MARKHUB_QUEUE_MAX) {
        $q = array_slice($q, -MARKHUB_QUEUE_MAX);
    }
    update_option(MARKHUB_QUEUE, $q, false);
    if (!wp_next_scheduled(MARKHUB_CRON)) {
        wp_schedule_event(time() + 300, 'markhub_five_minutes', MARKHUB_CRON);
    }
}

// --- capture: after the theme has handled the POST ----------------------------------------------------------------

$GLOBALS['markhub_connector_pending'] = null;

add_action('init', function () {
    if (empty($_SERVER['REQUEST_METHOD']) || $_SERVER['REQUEST_METHOD'] !== 'POST' || empty($_POST) || is_admin() && !wp_doing_ajax()) {
        return;
    }
    $s = markhub_connector_settings();
    if (!$s['enabled']) {
        return;
    }
    $post = $_POST; // phpcs:ignore WordPress.Security.NonceVerification -- read-only copy; the theme verifies its own forms
    $form = markhub_connector_match($post);
    if ($form === null) {
        return;
    }
    $payload = markhub_connector_payload($post, $form);
    if ($payload === null) {
        return;
    }
    // A double click or a resubmitted page sends the same form twice: forward it once.
    $dedupe = 'markhub_' . md5($form . '|' . strtolower($payload['email']) . '|' . $payload['source_page']);
    if (get_transient($dedupe)) {
        return;
    }
    set_transient($dedupe, 1, 2 * MINUTE_IN_SECONDS);
    $GLOBALS['markhub_connector_pending'] = $payload;
}, 0);

add_action('shutdown', function () {
    $payload = $GLOBALS['markhub_connector_pending'];
    if (!$payload) {
        return;
    }
    // The theme rejected it (validation, captcha): nothing to forward.
    $code = function_exists('http_response_code') ? (int) http_response_code() : 200;
    if ($code >= 400) {
        return;
    }
    // Finish the visitor's response first, so they never wait on MarkHub.
    if (function_exists('fastcgi_finish_request')) {
        fastcgi_finish_request();
        $result = markhub_connector_send($payload, 8);
    } else {
        $result = markhub_connector_send($payload, 3);
    }
    if ($result !== true) {
        markhub_connector_enqueue($payload);
    }
    markhub_connector_log($payload['form'], $payload['email'], $result === true ? true : 'queued (' . $result . ')');
}, 1);

// --- retry queue --------------------------------------------------------------------------------------------------

add_filter('cron_schedules', function ($s) {
    $s['markhub_five_minutes'] = array('interval' => 300, 'display' => 'Every five minutes (MarkHub)');
    return $s;
});

add_action(MARKHUB_CRON, 'markhub_connector_retry');

function markhub_connector_retry()
{
    $q = get_option(MARKHUB_QUEUE, array());
    if (!$q) {
        wp_clear_scheduled_hook(MARKHUB_CRON);
        return;
    }
    $left = array();
    foreach ($q as $i => $item) {
        if ($i >= 50) { // at most 50 per run; the rest wait for the next one
            $left[] = $item;
            continue;
        }
        $result = markhub_connector_send($item['payload'], 15);
        if ($result === true) {
            markhub_connector_log($item['payload']['form'], $item['payload']['email'], 'sent (retry)');
            continue;
        }
        $item['tries']++;
        // Keep trying for a week; after that the lead is still in the site's own records, but the log says so.
        if (time() - $item['since'] < 7 * DAY_IN_SECONDS) {
            $left[] = $item;
        } else {
            markhub_connector_log($item['payload']['form'], $item['payload']['email'], 'dropped after a week: ' . $result);
        }
    }
    update_option(MARKHUB_QUEUE, $left, false);
    if (!$left) {
        wp_clear_scheduled_hook(MARKHUB_CRON);
    }
}

register_deactivation_hook(__FILE__, function () {
    wp_clear_scheduled_hook(MARKHUB_CRON);
});

// --- Settings > MarkHub -------------------------------------------------------------------------------------------

add_action('admin_menu', function () {
    add_options_page('MarkHub', 'MarkHub', 'manage_options', 'markhub-connector', 'markhub_connector_page');
});

add_action('admin_init', function () {
    register_setting('markhub_connector', MARKHUB_OPT, array(
        'type' => 'array',
        'sanitize_callback' => function ($in) {
            $old = get_option(MARKHUB_OPT, array());
            return array(
                'api' => esc_url_raw(trim(isset($in['api']) ? $in['api'] : '')),
                // An empty key field keeps the saved key (it is never printed back into the page).
                'key' => !empty($in['key']) ? sanitize_text_field($in['key']) : (isset($old['key']) ? $old['key'] : ''),
                'enabled' => !empty($in['enabled']) ? 1 : 0,
            );
        },
    ));
});

add_action('admin_post_markhub_connector_test', function () {
    if (!current_user_can('manage_options')) {
        wp_die('Not allowed');
    }
    check_admin_referer('markhub_connector_test');
    $s = markhub_connector_settings();
    // An empty body: a valid key gets 422 (missing fields), a wrong key 401. Nothing is created either way.
    $r = wp_remote_post($s['api'] . '/api/integrations/wordpress/submissions', array(
        'timeout' => 60, // a sleeping free-tier API can take most of a minute to wake
        'headers' => array('Content-Type' => 'application/json', 'X-MarkHub-Key' => $s['key']),
        'body' => '{}',
    ));
    if (is_wp_error($r)) {
        $msg = 'Could not reach MarkHub: ' . $r->get_error_message();
    } else {
        $code = (int) wp_remote_retrieve_response_code($r);
        $msg = $code === 422 ? 'Connected: MarkHub accepted the key.' : ($code === 401 ? 'MarkHub rejected the key (check WP_INTAKE_SECRET).' :
            ($code === 503 ? 'MarkHub has no WP_INTAKE_SECRET set yet.' : 'Unexpected answer: HTTP ' . $code));
    }
    set_transient('markhub_connector_notice', $msg, 60);
    wp_safe_redirect(admin_url('options-general.php?page=markhub-connector'));
    exit;
});

add_action('admin_post_markhub_connector_flush', function () {
    if (!current_user_can('manage_options')) {
        wp_die('Not allowed');
    }
    check_admin_referer('markhub_connector_flush');
    markhub_connector_retry();
    set_transient('markhub_connector_notice', 'Retried the queue.', 60);
    wp_safe_redirect(admin_url('options-general.php?page=markhub-connector'));
    exit;
});

function markhub_connector_page()
{
    $s = markhub_connector_settings();
    $o = get_option(MARKHUB_OPT, array());
    $queue = get_option(MARKHUB_QUEUE, array());
    $log = get_option(MARKHUB_LOG, array());
    $notice = get_transient('markhub_connector_notice');
    delete_transient('markhub_connector_notice');
    ?>
    <div class="wrap">
        <h1>MarkHub</h1>
        <?php if ($notice) : ?><div class="notice notice-info"><p><?php echo esc_html($notice); ?></p></div><?php endif; ?>
        <p>Sends request-a-demo, contact-sales and gated-download submissions to MarkHub. The site's own handling of
            those forms is unchanged.</p>
        <form method="post" action="options.php">
            <?php settings_fields('markhub_connector'); ?>
            <table class="form-table" role="presentation">
                <tr><th scope="row"><label for="mh-api">MarkHub API address</label></th>
                    <td><input id="mh-api" class="regular-text" name="<?php echo esc_attr(MARKHUB_OPT); ?>[api]" value="<?php echo esc_attr($s['api']); ?>" <?php disabled(defined('MARKHUB_API_URL')); ?>></td></tr>
                <tr><th scope="row"><label for="mh-key">Intake key</label></th>
                    <td><input id="mh-key" class="regular-text" type="password" autocomplete="new-password" name="<?php echo esc_attr(MARKHUB_OPT); ?>[key]"
                               placeholder="<?php echo $s['key'] ? esc_attr__('Saved (leave empty to keep)') : ''; ?>" <?php disabled(defined('MARKHUB_INTAKE_KEY')); ?>>
                        <p class="description">The value of WP_INTAKE_SECRET in MarkHub's environment (Render).</p></td></tr>
                <tr><th scope="row">Forward submissions</th>
                    <td><label><input type="checkbox" name="<?php echo esc_attr(MARKHUB_OPT); ?>[enabled]" value="1" <?php checked(!empty($o['enabled']) || (defined('MARKHUB_ENABLED') && MARKHUB_ENABLED)); ?>> On</label></td></tr>
            </table>
            <?php submit_button(); ?>
        </form>
        <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>" style="display:inline">
            <input type="hidden" name="action" value="markhub_connector_test"><?php wp_nonce_field('markhub_connector_test'); ?>
            <?php submit_button('Test connection', 'secondary', 'submit', false); ?>
        </form>
        <?php if ($queue) : ?>
            <form method="post" action="<?php echo esc_url(admin_url('admin-post.php')); ?>" style="display:inline;margin-left:8px">
                <input type="hidden" name="action" value="markhub_connector_flush"><?php wp_nonce_field('markhub_connector_flush'); ?>
                <?php submit_button(sprintf('Retry %d queued now', count($queue)), 'secondary', 'submit', false); ?>
            </form>
        <?php endif; ?>
        <h2>Recent forwards</h2>
        <?php if (!$log) : ?><p>None yet.</p><?php else : ?>
            <table class="widefat striped"><thead><tr><th>When (UTC)</th><th>Form</th><th>Domain</th><th>Result</th></tr></thead><tbody>
            <?php foreach ($log as $row) : ?>
                <tr><td><?php echo esc_html($row['at']); ?></td><td><?php echo esc_html($row['form']); ?></td>
                    <td><?php echo esc_html($row['who']); ?></td><td><?php echo esc_html($row['result']); ?></td></tr>
            <?php endforeach; ?>
            </tbody></table>
        <?php endif; ?>
    </div>
    <?php
}
