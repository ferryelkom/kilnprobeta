#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════════════
#  KILNPRO BETA v6.6 — CERAMIC FIRING CONTROLLER (FIX RAMP LOGIC)
#  MicroPython | ESP32 | MAX31856 R-type | SSR 10s cycle
# ───────────────────────────────────────────────────────────────────────────────
#  [EMERGENCY-FIX] _ramp_duty(): error = target_now - cur_temp (negativ=ÎNAINTE)
#  Acum 336°C > 319°C target → error=-17°C → SSR OFF ✓

import time, gc, machine
from machine import Pin


# ─── SENSOR (ORIGINAL - FUNCȚIONEAZĂ BRICI) ───────────────────────────────────
def read_temp_precise():
    """Citire temperatura cu 2 zecimale, fara modificari librarie."""
    sensor = machine.max31856
    raw = sensor.status()['temp']
    return round(raw, 2)


# ─── PROFIL ARDERE ────────────────────────────────────────────────────────────
PROFILE = [
    {'id': 1, 'type': 'ramp', 'target': 150.0, 'duration_min': 60},
    {'id': 2, 'type': 'hold', 'target': 150.0, 'duration_min': 180},
    {'id': 3, 'type': 'ramp', 'target': 370.0, 'duration_min': 60},
    {'id': 4, 'type': 'hold', 'target': 370.0, 'duration_min': 120},
    {'id': 5, 'type': 'ramp', 'target': 750.0, 'duration_min': 120},
    {'id': 6, 'type': 'hold', 'target': 750.0, 'duration_min': 240},
]

COLD_THRESHOLD   = 40.0
RESUME_MARGIN    = 2.0
FRACTURE_DROP    = 9.0


# ─── LOGIC RESUME ─────────────────────────────────────────────────────────────
def find_resume_segment(cur_temp):
    if cur_temp < COLD_THRESHOLD:
        return {
            'seg_idx': 0, 'mode': 'startup_fresh',
            'ramp_start_t': cur_temp,
            'note': 'FRESH START | T=' + str(round(cur_temp, 1)) + 'C < ' + str(COLD_THRESHOLD) + 'C',
        }
    for idx, seg in enumerate(PROFILE):
        if cur_temp < seg['target'] - RESUME_MARGIN:
            mode = 'resume_ramp' if seg['type'] == 'ramp' else 'resume_hold'
            return {
                'seg_idx': idx, 'mode': mode,
                'ramp_start_t': cur_temp,
                'note': 'RESUME Seg' + str(seg['id']) + ' | T=' + str(round(cur_temp, 1)) + 'C → ' + str(seg['target']) + 'C',
            }
    last = PROFILE[-1]
    return {
        'seg_idx': len(PROFILE) - 1, 'mode': 'resume_hold',
        'ramp_start_t': cur_temp,
        'note': 'RESUME LAST Seg' + str(last['id']) + ' hold ' + str(last['target']) + 'C',
    }


# ─── CONTROLLER v6.6 (FIX RAMP LOGIC) ─────────────────────────────────────────
class CeramicKiln:

    def __init__(self):
        self.ssr          = Pin(32, Pin.OUT)
        self.ssr.off()
        self.cycle_time   = 10.0
        self.min_ssr_off  = 1.5

        # RAMP CONTROL - FIXED LOGIC
        self.ramp_ahead_band    = 0.5    # Tcur > target+0.5°C → OFF
        self.ramp_hold_band     = 1.0    # |error| <= 1.0°C → micro-duty
        self.ramp_micro_min     = 0.05
        self.ramp_micro_max     = 0.15
        self.ramp_prop_gain     = 0.03
        self.ramp_base_duty     = 0.40
        self.max_ramp_duty      = 0.80

        self.comp_max     = 0.15
        self.comp_gain    = 0.05
        self.history_len  = 5
        self.temp_history = []
        self.comp_history = []

        self.hold_duty_far   = 0.20
        self.hold_duty_mid   = 0.12
        self.hold_duty_close = 0.10
        self.hold_micro_min  = 0.03
        self.hold_micro_max  = 0.09

        self.pre_hold_margin_pct = 0.06
        self.pre_hold_min_duty   = 0.05
        self.slowdown_exp        = 1.8

        self.fracture_drop_limit = FRACTURE_DROP
        self.seg_temp_max        = None
        self.fracture_alarm_count = 0

        self.seg_idx          = 0
        self.seg_mode         = 'startup'
        self.program_complete = False
        self.resume_info      = None

        self.prog_start_time    = time.time()
        self.startup_start_time = None
        self.startup_duration   = 20.0
        self.ramp_start_time    = None
        self.ramp_start_temp    = None
        self.ramp_target_fixed  = None
        self.ramp_rate_fixed    = None
        self.hold_start_time    = None
        self.last_ssr_off_time  = 0
        self.last_comp_print_min = 0.0
        self.total_duration_min = sum(s['duration_min'] for s in PROFILE)

        self.last_base_duty      = 0.0
        self.compensation_factor = 0.0
        self.log_file            = 'ceramic_log.csv'
        self.log_append_mode     = False

    def begin(self):
        cur = read_temp_precise()
        self.resume_info = find_resume_segment(cur)
        self.seg_idx     = self.resume_info['seg_idx']
        self.log_append_mode = (self.resume_info['mode'] != 'startup_fresh')

        self._print_profile(cur)
        self._print_resume_banner(self.resume_info)
        self._log_header()

        self.startup_start_time = time.time()
        self.seg_mode = 'startup'
        mode = self.resume_info['mode']
        if mode == 'startup_fresh':
            print('\n🔍 STARTUP 20s | T=' + str(cur) + 'C → Seg1 RAMP 150C')
        else:
            print('\n🔍 STABILIZARE 20s | T=' + str(cur) + 'C → ' + self.resume_info['note'])

    def _print_profile(self, t_start):
        total = sum(s['duration_min'] for s in PROFILE)
        print('\n' + '=' * 96)
        print('  KILNPRO BETA v6.6 — PROFIL ARDERE CERAMICA (FIX RAMP)')
        print('=' * 96)
        cum = 0
        for i, s in enumerate(PROFILE):
            if s['type'] == 'ramp':
                t_from = t_start if i == 0 else PROFILE[i-1]['target']
                span = s['target'] - t_from
                if span > 0:
                    r = round(span / s['duration_min'], 2)
                    rate_str = 'Rata: ' + str(r) + 'C/min'
                else:
                    rate_str = 'Rata: N/A (T>target)'
            else:
                rate_str = 'Mentinere ±2C'
            print('  Seg ' + str(s['id']) + ' | ' + s['type'].upper() + ' | Target: ' + 
                  str(s['target']) + 'C | ' + str(s['duration_min']) + ' min | Start: ' +
                  str(cum) + ' min | Stop: ' + str(cum + s['duration_min']) + ' min | ' + rate_str)
            cum += s['duration_min']
        print('  TOTAL: ~' + str(total) + ' min ≈ ' + str(round(total/60, 1)) + ' ore')
        print('=' * 96)

    def _print_resume_banner(self, info):
        mode = info['mode']
        seg  = PROFILE[info['seg_idx']]
        icon = {'startup_fresh': 'NEW', 'resume_ramp': 'RESUME-RAMP', 'resume_hold': 'RESUME-HOLD'}.get(mode, 'RESUME')
        print('\n' + '-' * 80)
        print('  [' + icon + '] ' + info['note'])
        if mode != 'startup_fresh':
            skipped = ', '.join('Seg' + str(s['id']) for s in PROFILE[:info['seg_idx']])
            if skipped:
                print('  Segmente skipped: ' + skipped)
            if seg['type'] == 'ramp':
                span = seg['target'] - info['ramp_start_t']
                if span > 0:
                    rate = round(span / seg['duration_min'], 2)
                    eta  = round(span / rate, 0)
                    print('  Rata recalculata: ' + str(rate) + 'C/min | ETA: ' + str(int(eta)) + 'min')
            print('  Hold resume = durata completa ' + str(seg['duration_min']) + 'min (safe dupa power loss)')
        print('-' * 80)

    # ═══════════════════════════════════════════════════════════════════════════
    #  [EMERGENCY-FIX v6.6] RAMP DUTY - LOGICĂ CORECTATĂ
    # ═══════════════════════════════════════════════════════════════════════════
    def _ramp_duty(self, cur_temp, target_now, seg_target_final, ramp_rate, measured_rate):
        """FIX v6.6: error = target_now - cur_temp (negativ=ÎNAINTE, pozitiv=ÎN URMĂ)"""
        error = target_now - cur_temp  # ex: 319-336 = -17 (negativ=ÎNAINTE)
        
        # ZONA 1: Tcur > target_now + 0.5°C → SSR OFF (încetinire)
        if error < -self.ramp_ahead_band:  # 319-336=-17 < -0.5
            self.compensation_factor = 0.0
            self.last_base_duty = 0.0
            return 0.0, 0.0
        
        # ZONA 2: |error| <= 1.0°C → micro-duty (pe linie)
        if abs(error) <= self.ramp_hold_band:
            t = max(0.0, error) / self.ramp_hold_band  # pozitiv=în urmă
            duty = self.ramp_micro_min + t * (self.ramp_micro_max - self.ramp_micro_min)
            duty *= self._pre_hold_factor(cur_temp, seg_target_final)
            self.compensation_factor = 0.0
            self.last_base_duty = duty
            return duty, duty
        
        # ZONA 3: Tcur < target_now → HIGH duty (prinde din urmă)
        comp = self._adaptive_compensation(measured_rate, ramp_rate)
        self.compensation_factor = comp
        
        prop_duty = self.ramp_base_duty + error * self.ramp_prop_gain
        prop_duty *= (1.0 + comp)
        prop_duty *= self._pre_hold_factor(cur_temp, seg_target_final)
        
        max_d = (self.cycle_time - self.min_ssr_off) / self.cycle_time
        duty = min(max_d, max(0.15, prop_duty))
        self.last_base_duty = self.ramp_base_duty
        return duty, self.ramp_base_duty

    # ═══════════════════════════════════════════════════════════════════════════
    #  LOG & HELPERS (neschimbate)
    # ═══════════════════════════════════════════════════════════════════════════
    def _log_header(self):
        hdr = ('prog_elapsed_min,elapsed_s,seg_id,seg_type,'
               'cur_temp,target_now,error_c,rate_cpm,'
               'on_time_s,off_time_s,ssr_state,'
               'comp_pct,base_duty_pct,'
               'seg_elapsed_min,seg_remaining_min,'
               'prog_remaining_min,resume_mode,alarm\n')
        fmode = 'a' if self.log_append_mode else 'w'
        with open(self.log_file, fmode) as f:
            if self.log_append_mode:
                f.write('# RESUME | ' + self.resume_info['note'] + '\n')
            f.write(hdr)

    def _log_line(self, elapsed_s, seg, cur, tgt, err, rate,
                  on_t, off_t, ssr, comp, base_duty,
                  seg_el, seg_rem, prog_rem, prog_el, alarm=''):
        line = (str(round(prog_el, 2)) + ',' + str(int(elapsed_s)) + ',' +
                str(seg['id']) + ',' + seg['type'] + ',' +
                str(round(cur, 2)) + ',' + str(round(tgt, 2)) + ',' +
                str(round(err, 2)) + ',' + str(round(rate, 3)) + ',' +
                str(round(on_t, 2)) + ',' + str(round(off_t, 2)) + ',' +
                str(int(ssr)) + ',' +
                str(round(comp*100, 2)) + ',' + str(round(base_duty*100, 2)) + ',' +
                str(round(seg_el, 2)) + ',' + str(round(seg_rem, 2)) + ',' +
                str(round(prog_rem, 2)) + ',' +
                self.resume_info['mode'] + ',' + alarm + '\n')
        with open(self.log_file, 'a') as f:
            f.write(line)
            f.flush()

    def _update_history(self, temp):
        self.temp_history.append((time.time(), temp))
        if len(self.temp_history) > self.history_len * 2:
            self.temp_history.pop(0)

    def _calc_rate(self):
        if len(self.temp_history) < 2:
            return 0.0
        t0, temp0 = self.temp_history[0]
        tn, tempn = self.temp_history[-1]
        dt = (tn - t0) / 60.0
        return (tempn - temp0) / dt if dt > 0.001 else 0.0

    def _adaptive_compensation(self, measured_rate, ramp_rate):
        self.comp_history.append(measured_rate)
        if len(self.comp_history) > self.history_len:
            self.comp_history.pop(0)
        if len(self.comp_history) < 3:
            return 0.0
        avg_rate   = sum(self.comp_history) / len(self.comp_history)
        rate_error = ramp_rate - avg_rate
        comp       = rate_error * self.comp_gain
        return max(-self.comp_max, min(self.comp_max, comp))

    def _pre_hold_factor(self, cur_temp, seg_target_final):
        margin = seg_target_final * self.pre_hold_margin_pct
        dist   = seg_target_final - cur_temp
        if dist <= 0 or dist >= margin:
            return 1.0
        factor = (dist / margin) ** self.slowdown_exp
        return max(self.pre_hold_min_duty, factor)

    def _hold_duty(self, cur_temp, hold_target):
        self.compensation_factor = 0.0
        error = hold_target - cur_temp
        if error <= -2.0:
            return 0.0, 0.0
        if error <= 0.0:
            return 0.0, 0.0
        if error < 1.0:
            return self.hold_micro_min, self.hold_micro_min
        if error < 5.0:
            t = (error - 1.0) / 4.0
            d = self.hold_micro_min + t * (self.hold_micro_max - self.hold_micro_min)
            return d, d
        if error < 20.0:
            return self.hold_duty_close, self.hold_duty_close
        ratio = cur_temp / hold_target if hold_target > 0 else 0
        if ratio < 0.75:
            return self.hold_duty_far, self.hold_duty_far
        elif ratio < 0.95:
            return self.hold_duty_mid, self.hold_duty_mid
        else:
            return self.hold_duty_close, self.hold_duty_close

    def _check_fracture(self, cur_temp, seg_mode):
        if seg_mode not in ('ramp', 'hold'):
            self.seg_temp_max = cur_temp
            return ''
        if self.seg_temp_max is None:
            self.seg_temp_max = cur_temp
            return ''
        if cur_temp > self.seg_temp_max:
            self.seg_temp_max = cur_temp
            return ''
        drop = self.seg_temp_max - cur_temp
        if drop >= self.fracture_drop_limit:
            self.fracture_alarm_count += 1
            alarm_str = 'FRACTURE_RISK_DROP_' + str(round(drop, 1)) + 'C'
            self.seg_temp_max = cur_temp
            return alarm_str
        return ''

    def _ramp_target_now(self):
        if self.ramp_start_time is None or self.ramp_start_temp is None:
            return PROFILE[self.seg_idx]['target']
        elapsed_min = (time.time() - self.ramp_start_time) / 60.0
        tgt = self.ramp_start_temp + self.ramp_rate_fixed * elapsed_min
        return min(self.ramp_target_fixed, max(self.ramp_start_temp, tgt))

    def _start_segment(self, cur_temp):
        if self.seg_idx >= len(PROFILE):
            self.program_complete = True
            return
        seg = PROFILE[self.seg_idx]
        if seg['type'] == 'ramp':
            if cur_temp >= seg['target'] - RESUME_MARGIN:
                print('  SKIP Seg' + str(seg['id']) + ': T=' + str(round(cur_temp, 1)) +
                      'C >= target ' + str(seg['target']) + 'C')
                self._advance_segment(cur_temp)
                return
            self.seg_mode        = 'ramp'
            self.ramp_start_time = time.time()
            self.ramp_start_temp = cur_temp
            self.ramp_target_fixed = seg['target']
            span = seg['target'] - cur_temp
            self.ramp_rate_fixed = span / seg['duration_min'] if span > 0 else 0.0
            self.seg_temp_max    = cur_temp
            eta = int(span / self.ramp_rate_fixed) if self.ramp_rate_fixed > 0.001 else seg['duration_min']
            resume_tag = ' [RESUME]' if self.resume_info['mode'] != 'startup_fresh' else ''
            print('\n🚀 SEG' + str(seg['id']) + ' RAMP' + resume_tag + ' ' +
                  str(round(cur_temp, 1)) + 'C → ' + str(seg['target']) + 'C | ' +
                  str(round(self.ramp_rate_fixed, 2)) + 'C/min | ETA: ' + str(eta) + 'min')
        else:
            self._enter_hold(cur_temp)

    def _enter_hold(self, cur_temp):
        seg = PROFILE[self.seg_idx]
        self.seg_mode      = 'hold'
        self.hold_start_time = time.time()
        self.seg_temp_max  = cur_temp
        self.comp_history.clear()
        self.compensation_factor = 0.0
        resume_tag = ' [RESUME]' if self.resume_info['mode'] != 'startup_fresh' else ''
        print('\n🎯 SEG' + str(seg['id']) + ' HOLD' + resume_tag + ' ' +
              str(seg['target']) + 'C | Durata: ' + str(seg['duration_min']) +
              'min | T_intrare=' + str(round(cur_temp, 2)) + 'C')

    def _advance_segment(self, cur_temp):
        done = PROFILE[self.seg_idx]
        print('\n✅ SEG' + str(done['id']) + ' COMPLET | T=' + str(round(cur_temp, 2)) + 'C')
        self.seg_idx += 1
        if self.seg_idx >= len(PROFILE):
            self.program_complete = True
            print('\n🏁 PROGRAM COMPLET')
            return
        nxt = PROFILE[self.seg_idx]
        print('▶  SEG' + str(nxt['id']) + ' → ' + nxt['type'].upper() +
              ' ' + str(nxt['target']) + 'C | ' + str(nxt['duration_min']) + 'min')
        self.comp_history.clear()
        self.temp_history.clear()
        self.seg_temp_max = cur_temp
        self._start_segment(cur_temp)

    def _check_transitions(self, cur_temp):
        if self.program_complete:
            return
        if self.seg_mode == 'startup':
            if (time.time() - self.startup_start_time) >= self.startup_duration:
                self._start_segment(cur_temp)
            return
        if self.seg_idx >= len(PROFILE):
            self.program_complete = True
            return
        seg = PROFILE[self.seg_idx]
        if self.seg_mode == 'ramp':
            if cur_temp >= seg['target'] - RESUME_MARGIN:
                self._enter_hold(cur_temp)
            return
        if self.seg_mode == 'hold':
            elapsed = (time.time() - self.hold_start_time) / 60.0
            if max(0.0, seg['duration_min'] - elapsed) <= 0.05:
                self._advance_segment(cur_temp)

    def _seg_timing(self):
        seg = PROFILE[min(self.seg_idx, len(PROFILE)-1)]
        if self.seg_mode == 'hold' and self.hold_start_time:
            el = (time.time() - self.hold_start_time) / 60.0
        elif self.seg_mode == 'ramp' and self.ramp_start_time:
            el = (time.time() - self.ramp_start_time) / 60.0
        else:
            el = 0.0
        return el, max(0.0, seg['duration_min'] - el)

    def _prog_timing(self):
        el = (time.time() - self.prog_start_time) / 60.0
        return el, max(0.0, self.total_duration_min - el)

    def update(self):
        if self.program_complete:
            return False

        cur_temp = read_temp_precise()
        self._update_history(cur_temp)
        rate = self._calc_rate()
        now  = time.time()

        self._check_transitions(cur_temp)
        if self.program_complete:
            return False

        seg = PROFILE[min(self.seg_idx, len(PROFILE)-1)]

        alarm = self._check_fracture(cur_temp, self.seg_mode)
        if alarm:
            self.ssr.value(0)
            print('\n⚠️  ' + alarm + ' | T=' + str(round(cur_temp, 2)) +
                  'C | SSR OFF emergency | Seg' + str(seg['id']))

        if self.seg_mode == 'startup':
            target    = seg['target']
            on_time   = 0.0
            base_duty = 0.0

        elif self.seg_mode == 'ramp':
            target_now = self._ramp_target_now()
            target     = target_now
            duty, base_duty = self._ramp_duty(
                cur_temp, target_now, seg['target'], self.ramp_rate_fixed, rate
            )
            self.last_base_duty = base_duty
            if alarm:
                duty, base_duty = 0.0, 0.0
            on_time = duty * self.cycle_time

        elif self.seg_mode == 'hold':
            target    = seg['target']
            duty, base_duty = self._hold_duty(cur_temp, seg['target'])
            self.last_base_duty = base_duty
            if alarm:
                duty, base_duty = 0.0, 0.0
            on_time = duty * self.cycle_time

        else:
            target, on_time, base_duty = seg['target'], 0.0, 0.0

        if (now - self.last_ssr_off_time) < self.min_ssr_off:
            on_time = 0.0

        max_on   = self.cycle_time - self.min_ssr_off
        on_time  = min(on_time, max_on)
        off_time = max(self.min_ssr_off, self.cycle_time - on_time)
        error    = target - cur_temp

        prog_el, prog_rem = self._prog_timing()
        seg_el, seg_rem   = self._seg_timing()

        ssr_state = False
        if on_time > 0.1 and not alarm:
            duty_pct = (on_time / self.cycle_time) * 100.0
            comp_str = str(round(self.compensation_factor*100, 1))
            if self.compensation_factor >= 0:
                comp_str = '+' + comp_str
            print('🔥 SSR ON ' + str(round(on_time, 1)) + 's | T=' +
                  str(round(cur_temp, 2)) + 'C | ' + str(round(duty_pct, 0)) +
                  '% [' + comp_str + '%] | Base=' +
                  str(round(self.last_base_duty*100, 0)) + '%')
            self.ssr.value(1)
            time.sleep(on_time)
            self.ssr.value(0)
            self.last_ssr_off_time = time.time()
            time.sleep(off_time)
            ssr_state = True
        else:
            self.ssr.value(0)
            self.last_ssr_off_time = time.time()
            time.sleep(self.cycle_time)

        if prog_el - self.last_comp_print_min >= 5.0 and self.comp_history:
            avg_r = sum(self.comp_history) / len(self.comp_history)
            print('📊 COMP ' + str(round(self.compensation_factor*100, 1)) +
                  '% | Rate_avg=' + str(round(avg_r, 2)) + 'C/min | ' +
                  'Hist=' + str(len(self.comp_history)))
            self.last_comp_print_min = prog_el

        seg_label = 'S' + str(seg['id']) + '-' + self.seg_mode[:4].upper()
        comp_val  = round(self.compensation_factor*100, 1)
        comp_str  = ('+' if comp_val >= 0 else '') + str(comp_val) + '%'
        alarm_tag = ' ⚠️ ' + alarm if alarm else ''
        
        print_line = (str(int(prog_el)) + 'm | ' +
                     str(round(cur_temp, 2)) + ' | ' +
                     str(round(target, 2)) + ' | ' +
                     ('+' if error >= 0 else '') + str(round(error, 2)) + ' | ' +
                     ('+' if rate >= 0 else '') + str(round(rate, 2)) + ' | ' +
                     str(round(on_time, 1)) + 's | ' +
                     ('ON ' if ssr_state else 'OFF') + ' | ' +
                     seg_label + ' | ' +
                     'SegRem:' + str(round(seg_rem, 1)) + 'm | ' +
                     'ProgRem:' + str(round(prog_rem, 1)) + 'm | ' +
                     comp_str + alarm_tag)
        print(print_line)

        self._log_line(
            int(now - self.prog_start_time),
            seg, cur_temp, target, error, rate,
            on_time, off_time, ssr_state,
            self.compensation_factor, self.last_base_duty,
            seg_el, seg_rem, prog_rem, prog_el, alarm
        )

        gc.collect()
        return True

    def force_stop(self):
        self.ssr.value(0)
        prog_el = (time.time() - self.prog_start_time) / 60.0
        seg = PROFILE[min(self.seg_idx, len(PROFILE)-1)]
        print('\n🛑 STOP FORTAT | Seg' + str(seg['id']) + ' ' + self.seg_mode +
              ' | Elapsed: ' + str(round(prog_el, 1)) + 'min | Target: ' +
              str(seg['target']) + 'C' +
              (' | ALARME FRACTURA: ' + str(self.fracture_alarm_count)
               if self.fracture_alarm_count else ''))


# ─── ORCHESTRATOR ─────────────────────────────────────────────────────────────
class FiringController:

    def __init__(self):
        self.kiln = CeramicKiln()
        self.kiln.begin()
        self._print_header()

    def _print_header(self):
        print('\n' + '=' * 110)
        print('   MIN | Tcur    | Ttgt    |  ERR    | RATE  | ON    | SSR | SEG      | SegRem   | ProgRem  | COMP')
        print('=' * 110)

    def run(self):
        try:
            while True:
                if not self.kiln.update():
                    self.kiln.ssr.value(0)
                    prog_el = (time.time() - self.kiln.prog_start_time) / 60.0
                    print('\n' + '=' * 110)
                    print('  🏁 PROGRAM ARDERE COMPLET — SSR OFF')
                    print('  Durata: ' + str(round(prog_el, 1)) + ' min = ' +
                          str(round(prog_el/60, 2)) + ' ore')
                    if self.kiln.fracture_alarm_count:
                        print('  ⚠️  ALARME FRACTURA: ' + str(self.kiln.fracture_alarm_count))
                    print('=' * 110)
                    break
        except KeyboardInterrupt:
            self.kiln.force_stop()


if __name__ == '__main__':
    FiringController().run()
# fix fail crestere 1.46/ minut fata de 3.66,
