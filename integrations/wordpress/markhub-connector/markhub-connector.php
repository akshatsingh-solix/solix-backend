<?php
/**
 * Plugin Name:       MarkHub Connector
 * Description:       Sends www.solix.com form submissions (demo, contact sales, trials, gated downloads, partner, newsletter) to MarkHub, server to server, so every lead lands in the MarkHub inbox, gets scored and is linked to the visitor's browsing.
 * Version:           1.2.0
 * Requires at least: 6.0
 * Requires PHP:      7.4
 * Author:            Solix Technologies
 * License:           Proprietary
 *
 * How it works
 * - solix.com's forms reach the server two ways, and both are covered:
 *   1. Full-page posts back to the page (request a demo, gated white papers): fields at the top level of $_POST.
 *   2. jQuery AJAX posts to the theme's templates/ajax-*.php handlers (contact sales, ECS demo, free trials,
 *      partner, newsletter): fields nested under one key, e.g. contact_us[cts_email].
 *   For (2) the handler must load WordPress (require wp-load.php) for this plugin to run. If one doesn't, add one
 *   line after its successful save:  do_action('markhub_capture', 'contact-sales', $fields);
 * - It runs after the site's own handler, so the site's emails and the Digiprise hand-off are unchanged. Only
 *   submissions the handler accepted are forwarded: an HTTP error, or an AJAX reply of {"result": 0}, is skipped.
 * - The MarkHub tag adds a hidden "markhub_visitor_id" field to forms once the visitor has accepted analytics cookies
 *   in the CookieYes banner; it is passed on so MarkHub can link the lead to their browsing.
 * - If MarkHub can't be reached, the lead is queued and retried every five minutes (wp-cron) for a week.
 *   Settings > MarkHub shows the last forwards and tests the connection.
 * - More forms: the "markhub_connector_forms" filter (see markhub_connector_forms()).
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
        'api' => rtrim(defined('MARKHUB_API_URL') ? MARKHUB_API_URL : (isset($o['api']) ? $o['api'] : ''), '/'),
        'key' => defined('MARKHUB_INTAKE_KEY') ? MARKHUB_INTAKE_KEY : (isset($o['key']) ? $o['key'] : ''),
        'enabled' => defined('MARKHUB_ENABLED') ? (bool) MARKHUB_ENABLED : !empty($o['enabled']),
    );
}

/**
 * Known forms on www.solix.com (field names read from the live site, November 2026). Each entry:
 * - wrap:   the POST key holding the fields, for the AJAX handlers (absent for full-page forms)
 * - detect: a field that must be present
 * - type:   the MarkHub submission type
 * - map:    MarkHub field => WordPress field name(s), or "auto" to find email/name/company/phone by field name
 * - interest: a fixed product interest for forms that belong to one product
 * AJAX forms come first: their wrapper key is the surest signal.
 */
function markhub_connector_forms()
{
    $forms = array(
        'contact-sales' => array(
            'wrap' => 'contact_us',
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
        'ecs-demo' => array(
            'wrap' => 'ecs_finance_demo',
            'detect' => 'new_email',
            'type' => 'demo',
            'interest' => 'Enterprise Content Services',
            'map' => array(
                'email' => 'new_email',
                'name' => array('first_name', 'last_name'),
                'company' => 'company_name',
                'phone' => 'user_phone',
                'country' => 'user_country',
                'job_title' => 'job_title',
            ),
        ),
        'cdp-trial' => array(
            'wrap' => 'cdp_demo_getStarted',
            'detect' => 'new_email',
            'type' => 'trial',
            'interest' => 'Common Data Platform',
            'map' => array('email' => 'new_email'),
        ),
        'ecs-trial' => array(
            'wrap' => 'ecs_finance_getStarted',
            'detect' => 'new_email',
            'type' => 'trial',
            'interest' => 'Enterprise Content Services',
            'map' => array('email' => 'new_email'),
        ),
        'partner' => array('wrap' => 'partnerForm', 'type' => 'partner', 'map' => 'auto'),
        'newsletter' => array('wrap' => 'newsletterHomeForm', 'type' => 'newsletter', 'map' => 'auto'),
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

/** For forms built in JavaScript: the first field whose name says email and that holds an address. */
function markhub_connector_auto_email(array $src)
{
    foreach ($src as $k => $v) {
        if (is_string($k) && is_scalar($v) && stripos($k, 'mail') !== false && is_email(trim((string) $v))) {
            return $k;
        }
    }
    return null;
}

function markhub_connector_auto_map(array $src)
{
    $map = array('email' => markhub_connector_auto_email($src));
    $first = $last = $full = null;
    foreach (array_keys($src) as $k) {
        if (!is_string($k)) {
            continue;
        }
        $l = strtolower($k);
        if ($first === null && preg_match('/first_?name|fname/', $l)) {
            $first = $k;
        } elseif ($last === null && preg_match('/last_?name|lname|surname/', $l)) {
            $last = $k;
        } elseif ($full === null && preg_match('/(^|_)name$|full_?name/', $l)) {
            $full = $k;
        } elseif (!isset($map['company']) && preg_match('/company|organi[sz]ation|business/', $l)) {
            $map['company'] = $k;
        } elseif (!isset($map['phone']) && preg_match('/phone|mobile|tel/', $l)) {
            $map['phone'] = $k;
        } elseif (!isset($map['job_title']) && preg_match('/title|designation|role/', $l)) {
            $map['job_title'] = $k;
        } elseif (!isset($map['country']) && strpos($l, 'country') !== false) {
            $map['country'] = $k;
        } elseif (!isset($map['message']) && preg_match('/message|msg|comment/', $l)) {
            $map['message'] = $k;
        }
    }
    $map['name'] = ($first || $last) ? array_values(array_filter(array($first, $last))) : $full;
    return array_filter($map);
}

/** Which known form this POST is: array(form id, its fields), or null. */
function markhub_connector_match(array $post)
{
    foreach (markhub_connector_forms() as $id => $form) {
        if (!empty($form['wrap'])) {
            if (empty($post[$form['wrap']]) || !is_array($post[$form['wrap']])) {
                continue;
            }
            $src = $post[$form['wrap']];
        } else {
            $src = $post;
        }
        if (!empty($form['detect']) && empty($src[$form['detect']])) {
            continue;
        }
        if ($form['map'] === 'auto' && markhub_connector_auto_email($src) === null) {
            continue;
        }
        if (!empty($form['requires'])) {
            foreach ($form['requires'] as $f) {
                if (empty($src[$f])) {
                    continue 2;
                }
            }
        }
        return array($id, $src);
    }
    return null;
}

function markhub_connector_field(array $post, $names, $max = 500)
{
    $parts = array();
    foreach ((array) $names as $n) {
        if (isset($post[$n]) && is_scalar($post[$n])) {
            $v = trim(sanitize_text_field((string) $post[$n]));
            if ($v !== '') {
                $parts[] = $v;
            }
        }
    }
    $v = implode(' ', $parts);
    return $v === '' ? null : mb_substr($v, 0, $max);
}

/** The MarkHub payload for one form's fields, or null when there is no usable email. */
function markhub_connector_payload(array $fields, $form_id)
{
    $forms = markhub_connector_forms();
    $form = $forms[$form_id];
    $limits = array('message' => 5000, 'country' => 80, 'interest' => 300);
    $p = array('type' => $form['type'], 'form' => $form_id);
    if (!empty($form['interest'])) {
        $p['interest'] = $form['interest'];
    }
    $map = $form['map'] === 'auto' ? markhub_connector_auto_map($fields) : $form['map'];
    foreach ($map as $to => $from) {
        $v = markhub_connector_field($fields, $from, isset($limits[$to]) ? $limits[$to] : 300);
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
    // AJAX forms post to admin-ajax.php or the theme's templates/ajax-*.php: the page they were on is the form's own
    // page_url field, else the referer.
    $ajax = strpos($uri, 'admin-ajax.php') !== false || strpos($uri, '/wp-content/') !== false;
    $referer = isset($_SERVER['HTTP_REFERER']) ? esc_url_raw(wp_unslash($_SERVER['HTTP_REFERER'])) : '';
    $given = markhub_connector_field($fields, 'page_url', 300);
    if ($ajax) {
        $page = ($given && strpos($given, 'http') === 0) ? $given : ($referer !== '' ? $referer : $scheme . $host . $uri);
    } else {
        $page = $scheme . $host . $uri;
    }
    $p['source_page'] = mb_substr(strtok($page, '?'), 0, 300);
    if ($form['type'] === 'download') {
        $path = trim((string) parse_url($p['source_page'], PHP_URL_PATH), '/');
        $slug = $path === '' ? '' : basename($path);
        if ($slug !== '') {
            $p['resource_slug'] = mb_substr(sanitize_title($slug), 0, 120);
            $p['resource'] = mb_substr(ucwords(str_replace('-', ' ', $slug)), 0, 300);
        }
    }
    $vid = markhub_connector_field($fields, 'markhub_visitor_id', 64);
    if (!$vid && isset($_POST['markhub_visitor_id'])) { // phpcs:ignore WordPress.Security.NonceVerification
        $vid = markhub_connector_field(wp_unslash($_POST), 'markhub_visitor_id', 64);
    }
    if ($vid && preg_match('/^[A-Za-z0-9_-]{8,64}$/', $vid)) {
        $p['visitor_id'] = $vid;
    }
    $ip = markhub_connector_client_ip();
    if ($ip !== '') {
        $p['client_ip'] = mb_substr($ip, 0, 64);
    }
    // The MarkHub tag adds markhub_internal=1 in browsers staff marked with ?solix_internal=1.
    $flag = markhub_connector_field($fields, 'markhub_internal', 2);
    if ($flag === null && isset($_POST['markhub_internal'])) { // phpcs:ignore WordPress.Security.NonceVerification
        $flag = markhub_connector_field(wp_unslash($_POST), 'markhub_internal', 2);
    }
    if ($flag === '1') {
        $p['internal_device'] = true;
    }
    return apply_filters('markhub_connector_payload', array_filter($p, function ($v) { return $v !== null && $v !== ''; }), $form_id, $fields);
}

/**
 * The visitor's address, used by MarkHub only to recognise Solix's office and VPN ranges (never stored).
 * Behind a load balancer or proxy, REMOTE_ADDR is the proxy: then the right-most public address in
 * X-Forwarded-For is the visitor (entries further left can be forged by the client). A trusted edge header can be
 * named instead: define('MARKHUB_CLIENT_IP_HEADER', 'HTTP_CF_CONNECTING_IP');
 */
function markhub_connector_client_ip()
{
    $public = function ($ip) {
        return (bool) filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE);
    };
    if (defined('MARKHUB_CLIENT_IP_HEADER') && !empty($_SERVER[MARKHUB_CLIENT_IP_HEADER])) {
        $parts = explode(',', sanitize_text_field(wp_unslash($_SERVER[MARKHUB_CLIENT_IP_HEADER])));
        $ip = trim($parts[0]);
        if (filter_var($ip, FILTER_VALIDATE_IP)) {
            return $ip;
        }
    }
    $remote = isset($_SERVER['REMOTE_ADDR']) ? sanitize_text_field(wp_unslash($_SERVER['REMOTE_ADDR'])) : '';
    if ($remote !== '' && $public($remote)) {
        return $remote;
    }
    if (!empty($_SERVER['HTTP_X_FORWARDED_FOR'])) {
        $chain = array_map('trim', explode(',', sanitize_text_field(wp_unslash($_SERVER['HTTP_X_FORWARDED_FOR']))));
        for ($i = count($chain) - 1; $i >= 0; $i--) {
            if ($public($chain[$i])) {
                return $chain[$i];
            }
        }
    }
    return $remote;
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

// --- capture: after the site's own handler has run ----------------------------------------------------------------

$GLOBALS['markhub_connector_pending'] = null;
$GLOBALS['markhub_connector_output'] = '';

add_action('init', function () {
    if (empty($_SERVER['REQUEST_METHOD']) || $_SERVER['REQUEST_METHOD'] !== 'POST' || empty($_POST) || (is_admin() && !wp_doing_ajax())) {
        return;
    }
    $match = markhub_connector_match(wp_unslash($_POST)); // phpcs:ignore WordPress.Security.NonceVerification -- read-only; the theme verifies its own forms
    if ($match === null) {
        return;
    }
    markhub_connector_queue_capture($match[0], $match[1]);
}, 0);

/**
 * Remember one submission to forward at shutdown (deduplicated). Also the entry point for theme handlers that run
 * without WordPress hooks: after a successful save they can call
 *   do_action('markhub_capture', 'contact-sales', $fields);
 */
function markhub_connector_queue_capture($form, array $fields)
{
    $s = markhub_connector_settings();
    $forms = markhub_connector_forms();
    if (!$s['enabled'] || !isset($forms[$form])) {
        return;
    }
    $payload = markhub_connector_payload($fields, $form);
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
    // The theme's AJAX handlers answer {"result": 1, ...}: keep a copy of the answer so only successes are forwarded.
    $GLOBALS['markhub_connector_output'] = '';
    ob_start(function ($buffer) {
        $GLOBALS['markhub_connector_output'] .= $buffer;
        return $buffer;
    });
}

add_action('markhub_capture', function ($form, $fields = array()) {
    markhub_connector_queue_capture((string) $form, is_array($fields) ? $fields : array());
}, 10, 2);

add_action('shutdown', function () {
    $payload = $GLOBALS['markhub_connector_pending'];
    if (!$payload) {
        return;
    }
    // The site rejected it (validation, captcha): nothing to forward.
    $code = function_exists('http_response_code') ? (int) http_response_code() : 200;
    if ($code >= 400) {
        return;
    }
    $reply = json_decode(trim((string) $GLOBALS['markhub_connector_output']), true);
    if (is_array($reply) && array_key_exists('result', $reply) && empty($reply['result'])) {
        return; // an AJAX handler said no: not a lead
    }
    // Finish the visitor's response first, so they never wait on MarkHub.
    while (ob_get_level() > 0) {
        ob_end_flush();
    }
    if (function_exists('fastcgi_finish_request')) {
        fastcgi_finish_request();
        $result = markhub_connector_send($payload, 8);
    } elseif (function_exists('litespeed_finish_request')) {
        litespeed_finish_request();
        $result = markhub_connector_send($payload, 8);
    } else {
        // mod_php can't close the connection early: don't make the visitor wait, queue it for wp-cron instead.
        $result = 'deferred';
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
            markhub_connector_log($item['payload']['form'], $item['payload']['email'], 'sent (queued)');
            continue;
        }
        $item['tries']++;
        // Keep trying for a week; the lead is safe in the site's own records and Digiprise either way.
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
        'timeout' => 60,
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
        <p>Copies demo, contact, trial, gated-download, partner and newsletter submissions to MarkHub. The site's own
            handling of those forms, its emails and the Digiprise hand-off are unchanged.</p>
        <form method="post" action="options.php">
            <?php settings_fields('markhub_connector'); ?>
            <table class="form-table" role="presentation">
                <tr><th scope="row"><label for="mh-api">MarkHub API address</label></th>
                    <td><input id="mh-api" class="regular-text" placeholder="https://markhub-api.solix.com" name="<?php echo esc_attr(MARKHUB_OPT); ?>[api]" value="<?php echo esc_attr($s['api']); ?>" <?php disabled(defined('MARKHUB_API_URL')); ?>></td></tr>
                <tr><th scope="row"><label for="mh-key">Intake key</label></th>
                    <td><input id="mh-key" class="regular-text" type="password" autocomplete="new-password" name="<?php echo esc_attr(MARKHUB_OPT); ?>[key]"
                               placeholder="<?php echo $s['key'] ? esc_attr__('Saved (leave empty to keep)') : ''; ?>" <?php disabled(defined('MARKHUB_INTAKE_KEY')); ?>>
                        <p class="description">The value of WP_INTAKE_SECRET in MarkHub's environment.</p></td></tr>
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
                <?php submit_button(sprintf('Send %d queued now', count($queue)), 'secondary', 'submit', false); ?>
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
