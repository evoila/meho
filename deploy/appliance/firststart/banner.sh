#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
#
# banner.sh: write the appliance's console banner, /etc/issue, which the VM
# console shows above the login prompt (form-1 design, section 2, step 9).
# meho-banner.service runs it at every boot, after first start, so the
# password warning below stays current.
#
# What it shows:
#   - while first start is not done: the [FAIL] line of the step that
#     stopped, or that first start has not finished
#   - when it is done: the console address, this appliance's IPv4 address
#     (agetty's \4, so a DHCP address shows too), the one-time files that
#     are still there, and a warning when the admin password expires in
#     less than 30 days
#   - it tells the admin to log in only when the admin account takes a
#     login; a locked or disabled account gets the way in through the boot
#     menu instead
#
# /etc/issue can be read by everyone, so it never holds a secret.

set -Eeuo pipefail

HOST_ROOT="${MEHO_TEST_ROOT:-}"

readonly ISSUE_FILE=/etc/issue
readonly DONE_FILE=/var/lib/meho/firststart.done
readonly FAILED_FILE=/var/lib/meho/firststart/failed
readonly VERSION_FILE=/opt/meho/appliance/download/VERSION
readonly ONE_TIME_FILES=(/var/lib/meho/first-login-passwords /var/lib/meho/unseal-key /var/lib/meho/bootloader-password)
readonly ADMIN_USER=admin
readonly WARN_DAYS=30
readonly BOOT_MENU_DOC="https://github.com/evoila/meho/blob/main/docs/codebase/appliance-firststart.md"

# admin_can_log_in: whether the admin account takes a login, as pam_unix
# decides it (check_shadow_expiry): it has a password that is not locked
# (! or *), the account has not expired, and the password did not expire
# longer ago than its inactive days.
admin_can_log_in() {
    local password="" last_change max_days inactive today
    IFS=: read -r _ password last_change _ max_days _ inactive _ < <(getent shadow "$ADMIN_USER") || true
    [[ -n $password && $password != [*!]* ]] || return 1
    ! admin_account_expired || return 1
    today=$(($(date +%s) / 86400))
    if [[ $last_change =~ ^[1-9][0-9]*$ && $max_days =~ ^[0-9]+$ && $inactive =~ ^[0-9]+$ ]]; then
        ((today - last_change <= max_days + inactive)) || return 1
    fi
    return 0
}

# admin_account_expired: whether the admin account has passed its expiry
# date (chage -E). A new password does not change that date.
admin_account_expired() {
    local expire=""
    IFS=: read -r _ _ _ _ _ _ _ expire _ < <(getent shadow "$ADMIN_USER") || true
    [[ $expire =~ ^[0-9]+$ ]] && (($(date +%s) / 86400 >= expire))
}

# days_until_password_expiry: the days until the admin password expires, or
# nothing when it never does or must be changed at the next login anyway.
days_until_password_expiry() {
    local last_change max_days today
    IFS=: read -r _ _ last_change _ max_days _ < <(getent shadow "$ADMIN_USER" || true)
    [[ $last_change =~ ^[1-9][0-9]*$ && $max_days =~ ^[0-9]+$ ]] || return 0
    today=$(($(date +%s) / 86400))
    printf '%d' $((last_change + max_days - today))
}

banner() {
    local version hostname file days reason fix
    version=$(sed -n 's/^MEHO_VERSION=//p' "${HOST_ROOT}${VERSION_FILE}" 2>/dev/null || true)
    printf 'MEHO appliance %s\n\n' "$version"
    if [[ -f ${HOST_ROOT}${FAILED_FILE} ]]; then
        printf 'First start stopped:\n'
        # agetty reads a backslash as the start of an escape.
        sed 's/\\/\\\\/g' "${HOST_ROOT}${FAILED_FILE}"
        # Step 3 gives the account its password; before that, nobody can log in.
        if admin_can_log_in; then
            printf '\nLog in as %s to fix it, or see: journalctl -u meho-firststart\n\n' "$ADMIN_USER"
        else
            printf '\nNobody can log in as %s yet. Follow the fix above from vCenter.\n\n' "$ADMIN_USER"
        fi
        return 0
    fi
    if [[ ! -f ${HOST_ROOT}${DONE_FILE} ]]; then
        printf 'First start has not finished. See: journalctl -u meho-firststart\n\n'
        return 0
    fi
    hostname=$(cat "${HOST_ROOT}/etc/hostname")
    printf '  Console:  https://%s/ui/\n' "$hostname"
    printf '  Address:  \\4\n'
    for file in "${ONE_TIME_FILES[@]}"; do
        if [[ -e ${HOST_ROOT}${file} ]]; then
            printf '  One-time file (root only; copy it to your key store, then delete it): %s\n' "$file"
        fi
    done
    days=$(days_until_password_expiry)
    if ! admin_can_log_in; then
        reason="its password is locked, or the account is disabled"
        fix="set a new password (passwd $ADMIN_USER)"
        if admin_account_expired; then
            reason="the account has expired"
            fix="remove its expiry date (chage -E -1 $ADMIN_USER) and set a new password (passwd $ADMIN_USER)"
        fi
        printf '\n  [WARN] Nobody can log in as %s: %s.\n' "$ADMIN_USER" "$reason"
        printf '  To get back in, restart the VM, open the boot menu with the bootloader password from your key store,\n'
        printf '  and %s. The steps: %s,\n' "$fix" "$BOOT_MENU_DOC"
        printf '  section "The boot menu, after a lost password or a locked admin".\n'
    elif [[ -n $days ]] && ((days < WARN_DAYS)); then
        if ((days < 0)); then
            printf '\n  [WARN] The password of %s has expired. Log in and set a new one before the account is disabled.\n' "$ADMIN_USER"
        else
            printf '\n  [WARN] The password of %s expires in %d days. Log in and set a new one.\n' "$ADMIN_USER" "$days"
        fi
    fi
    printf '\n'
}

temporary=$(mktemp "${HOST_ROOT}${ISSUE_FILE}.XXXXXX")
banner >"$temporary"
chmod 0644 "$temporary"
mv -f "$temporary" "${HOST_ROOT}${ISSUE_FILE}"
