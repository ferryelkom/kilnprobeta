#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════════════
#  KILNPRO BETA v6.3 — CERAMIC FIRING CONTROLLER
#  MicroPython | ESP32 | MAX31856 R-type | SSR 10s cycle
# ───────────────────────────────────────────────────────────────────────────────
#  ISTORICUL FIXURILOR:
#
#  vs beta521:
#  [FIX-1] Compensation sign corectat: rate_error = ramp_rate - avg_rate
#  [FIX-2] Tranzitie HOLD pe temperatura REALA (nu pe timp)
#  [FIX-3] Base duties marite: 65/55/45% (era 60/50/40)
#  [FIX-4] Hold duty redus + proportional cu micro-pulses
#  [FIX-5] min_ssr_off=1.5s (previne flicker lumini + racire SSR)
#  [FIX-6] Pre-hold slowdown quadratic anti-overshoot ceramic
#
#  vs beta610:
#  [FIX-7] Auto-resume: detectare segment corect pe baza T_start la boot
#  [FIX-8] Position-following ramp: SSR OFF cand cur > target_now
#  [FIX-9] Fracture protection: alarma la cadere >9C
#
#  vs beta620cufix (BUG CRITIC REZOLVAT):
#  [FIX-10] RESUME RAMP: rata ramane ORIGINALA a segmentului (nu recalculata!)
#           ramp_start_time = now - elapsed_deja_pe_rampa (BACKTRACK)
#           ramp_start_temp = originea segmentului (target seg anterior)
#           CAUZA PROBLEMEI: beta620 calcula 36.4C/60min=0.61C/min in loc de
#           3.67C/min → target_now urca lent → inerție termică depasea constant
#           target_now → SSR OFF permanent → 1.41C/min in loc de 3.67C/min
#           → ceramica arsa gresit, risc fractura termica
#
#  PROFILE: 6 segmente ardere ceramica ~13 ore
#  Seg 1 | RAMP  | T_start → 150C  |  60 min  | rata variabila
#  Seg 2 | HOLD  | 150C            | 180 min  | 3h
#  Seg 3 | RAMP  | 150C → 370C     |  60 min  | 3.67C/min
#  Seg 4 | HOLD  | 370C            | 120 min  | 2h
#  Seg 5 | RAMP  | 370C → 750C     | 120 min  | 3.17C/min
#  Seg 6 | HOLD  | 750C            | 240 min  | 4h
# ═══════════════════════════════════════════════════════════════════════════════

import time, gc, machine
from machine import Pin


# MicroPython nu are .ljust/.rjust pe string
def rj(v, w):
    s = str(v)
    return ' ' * max(0, w - len(s)) + s

def lj(v, w):
    s = str(v)
    return s + ' ' * max(0, w - len(s))



# ─── SENSOR ───────────────────────────────────────────────────────────────────
def read_temp_precise():
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

COLD_THRESHOLD = 40.0   # sub 40C = fresh start
RESUME_MARGIN  = 2.0    # histereza tranzitie hold
FRACTURE_DROP  = 9.0    # C - cadere maxima admisa fara alarma


# ─── HELPER: temperatura de origine a unui segment ramp ───────────────────────
def _seg_origin_temp(idx, cur_temp_at_boot):
    """
    Temperatura de start a segmentului idx (adica de unde porneste rampa).
    Seg 1: de la temperatura curenta (variabila).
    Seg 3, 5: de la target-ul segmentului anterior.
    """
    if idx == 0:
        return cur_temp_at_boot
    return PROFILE[idx - 1]['target']


# ─── LOGIC RESUME ─────────────────────────────────────────────────────────────
def find_resume_segment(cur_temp):
    """
    Detecteaza automat segmentul de start la boot pe baza T curente.
    Cauta primul segment unde T < target - RESUME_MARGIN.
    """
    if cur_temp < COLD_THRESHOLD:
        return {
            'seg_idx': 0, 'mode': 'startup_fresh',
            'boot_temp': cur_temp,
            'note': 'FRESH START | T=' + str(round(cur_temp, 1)) + 'C',
        }
    for idx, seg in enumerate(PROFILE):
        if cur_temp < seg['target'] - RESUME_MARGIN:
            mode = 'resume_ramp' if seg['type'] == 'ramp' else 'resume_hold'
            return {
                'seg_idx': idx, 'mode': mode,
                'boot_temp': cur_temp,
                'note': 'RESUME Seg' + str(seg['id']) + ' | T=' +
                        str(round(cur_temp, 1)) + 'C → ' + str(seg['target']) + 'C',
            }
    last = PROFILE[-1]
    return {
        'seg_idx': len(PROFILE) - 1, 'mode': 'resume_hold',
        'boot_temp': cur_temp,
        'note': 'RESUME LAST Seg' + str(last['id']) + ' hold ' + str(last['target']) + 'C',
    }


# ─── CONTROLLER ───────────────────────────────────────────────────────────────
class CeramicKiln:

    def __init__(self):
        # Hardware
        self.ssr         = Pin(32, Pin.OUT)
        self.ssr.off()
        self.cycle_time  = 10.0
        self.min_ssr_off = 1.5    # FIX-5: previne flicker + racire SSR

        # FIX-8: Parametri position-following ramp
        self.ramp_ahead_band = 0.5    # cur > target_now+0.5 → OFF
        self.ramp_hold_band  = 1.0    # |error| <= 1C → micro-duty
        self.ramp_micro_min  = 0.05   # 5% duty pe linie
        self.ramp_micro_max  = 0.15   # 15% duty pe linie
        self.ramp_prop_gain  = 0.03   # 3% duty per 1C deficit
        self.ramp_base_duty  = 0.40   # duty de baza in urma rampei
        self.max_ramp_duty   = 0.80   # duty maxim ramp

        # Compensatie rata (secundara, ±15%)
        self.comp_max    = 0.15
        self.comp_gain   = 0.05
        self.history_len = 5
        self.temp_history = []
        self.comp_history = []

        # FIX-4: Hold duty proportional
        self.hold_duty_far   = 0.20
        self.hold_duty_mid   = 0.12
        self.hold_duty_close = 0.10
        self.hold_micro_min  = 0.03
        self.hold_micro_max  = 0.09

        # FIX-6: Anti-overshoot pre-hold
        self.pre_hold_margin_pct = 0.06
        self.pre_hold_min_duty   = 0.05
        self.slowdown_exp        = 1.8

        # FIX-9: Fracture protection
        self.fracture_drop_limit  = FRACTURE_DROP
        self.seg_temp_max         = None
        self.fracture_alarm_count = 0

        # State machine
        self.seg_idx          = 0
        self.seg_mode         = 'startup'
        self.program_complete = False
        self.resume_info      = None

        # Timing
        self.prog_start_time     = time.time()
        self.startup_start_time  = None
        self.startup_duration    = 20.0
        self.ramp_start_time     = None
        self.ramp_start_temp     = None   # originea segmentului (nu cur_temp la resume)
        self.ramp_target_fixed   = None
        self.ramp_rate_fixed     = None   # rata ORIGINALA a segmentului
        self.hold_start_time     = None
        self.last_ssr_off_time   = 0
        self.last_comp_print_min = 0.0
        self.total_duration_min  = sum(s['duration_min'] for s in PROFILE)

        # Debug / log
        self.last_base_duty      = 0.0
        self.compensation_factor = 0.0
        self.log_file            = 'ceramic_log.csv'
        self.log_append_mode     = False

    # ─── INITIALIZARE ──────────────────────────────────────────────────────────
    def begin(self):
        cur = read_temp_precise()
        self.resume_info     = find_resume_segment(cur)
        self.seg_idx         = self.resume_info['seg_idx']
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
            print('\n🔍 STABILIZARE 20s | T=' + str(cur) + 'C | ' + self.resume_info['note'])

    # ─── PRINT PROFIL ─────────────────────────────────────────────────────────
    def _print_profile(self, t_start):
        total = sum(s['duration_min'] for s in PROFILE)
        print('\n' + '=' * 96)
        print('  KILNPRO BETA v6.3 — PROFIL ARDERE CERAMICA')
        print('=' * 96)
        cum = 0
        for i, s in enumerate(PROFILE):
            if s['type'] == 'ramp':
                origin = t_start if i == 0 else PROFILE[i-1]['target']
                span = s['target'] - origin
                if span > 0:
                    rate_str = 'Rata: ' + str(round(span / s['duration_min'], 2)) + 'C/min'
                else:
                    rate_str = 'Rata: N/A (T>target)'
            else:
                rate_str = 'Mentinere +-2C'
            print('  Seg ' + str(s['id']) + ' | ' + lj(s["type"].upper(), 4) +
                  ' | Target: ' + rj(s["target"], 5) + 'C | ' +
                  rj(s["duration_min"], 4) + ' min | ' +
                  'Start: ' + rj(cum, 5) + ' min | ' +
                  'Stop: ' + rj(cum + s["duration_min"], 5) + ' min | ' + rate_str)
            cum += s['duration_min']
        print('  TOTAL: ~' + str(total) + ' min = ' + str(round(total/60, 1)) + ' ore')
        print('=' * 96)

    # ─── PRINT RESUME BANNER ──────────────────────────────────────────────────
    def _print_resume_banner(self, info):
        mode = info['mode']
        seg  = PROFILE[info['seg_idx']]
        icons = {'startup_fresh': 'NEW', 'resume_ramp': 'RESUME-RAMP', 'resume_hold': 'RESUME-HOLD'}
        icon = icons.get(mode, 'RESUME')
        print('\n' + '-' * 80)
        print('  [' + icon + '] ' + info['note'])
        if mode != 'startup_fresh':
            skipped = ', '.join('Seg' + str(s['id']) for s in PROFILE[:info['seg_idx']])
            if skipped:
                print('  Segmente skipped: ' + skipped)
            if seg['type'] == 'ramp':
                origin = _seg_origin_temp(info['seg_idx'], info['boot_temp'])
                span_total = seg['target'] - origin
                if span_total > 0:
                    orig_rate = round(span_total / seg['duration_min'], 2)
                    span_rem  = seg['target'] - info['boot_temp']
                    eta_min   = int(span_rem / orig_rate) if orig_rate > 0.001 else 0
                    elapsed_already = int((info['boot_temp'] - origin) / orig_rate) if orig_rate > 0.001 else 0
                    print('  Rata ORIGINALA segm: ' + str(orig_rate) + 'C/min (PASTRATA)')
                    print('  Deja parcurs: ~' + str(elapsed_already) + 'min | SegRem: ~' + str(eta_min) + 'min')
            print('  NOTA: Hold resume = durata completa (safe dupa power loss)')
        print('-' * 80)

    # ─── LOG ──────────────────────────────────────────────────────────────────
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
                  on_t, off_t, ssr, comp, base_d,
                  seg_el, seg_rem, prog_rem, prog_el, alarm):
        line = (str(round(prog_el, 2)) + ',' + str(int(elapsed_s)) + ',' +
                str(seg['id']) + ',' + seg['type'] + ',' +
                str(round(cur, 2)) + ',' + str(round(tgt, 2)) + ',' +
                str(round(err, 2)) + ',' + str(round(rate, 3)) + ',' +
                str(round(on_t, 2)) + ',' + str(round(off_t, 2)) + ',' +
                str(int(ssr)) + ',' +
                str(round(comp * 100, 2)) + ',' + str(round(base_d * 100, 2)) + ',' +
                str(round(seg_el, 2)) + ',' + str(round(seg_rem, 2)) + ',' +
                str(round(prog_rem, 2)) + ',' +
                self.resume_info['mode'] + ',' + alarm + '\n')
        with open(self.log_file, 'a') as f:
            f.write(line)
            f.flush()

    # ─── HISTORY & RATA ───────────────────────────────────────────────────────
    def _update_history(self, temp):
        self.temp_history.append((time.time(), temp))
        if len(self.temp_history) > self.history_len * 2:
            self.temp_history.pop(0)

    def _calc_rate(self):
        if len(self.temp_history) < 2:
            return 0.0
        t0, v0 = self.temp_history[0]
        tn, vn = self.temp_history[-1]
        dt = (tn - t0) / 60.0
        return (vn - v0) / dt if dt > 0.001 else 0.0

    # ─── FIX-1: COMPENSATIE RATA ──────────────────────────────────────────────
    def _adaptive_compensation(self, measured_rate, ramp_rate):
        self.comp_history.append(measured_rate)
        if len(self.comp_history) > self.history_len:
            self.comp_history.pop(0)
        if len(self.comp_history) < 3:
            return 0.0
        avg = sum(self.comp_history) / len(self.comp_history)
        comp = (ramp_rate - avg) * self.comp_gain   # FIX-1: semn corectat
        return max(-self.comp_max, min(self.comp_max, comp))

    # ─── FIX-6: FACTOR FRANARE PRE-HOLD ──────────────────────────────────────
    def _pre_hold_factor(self, cur_temp, seg_target_final):
        margin = seg_target_final * self.pre_hold_margin_pct
        dist   = seg_target_final - cur_temp
        if dist <= 0 or dist >= margin:
            return 1.0
        return max(self.pre_hold_min_duty, (dist / margin) ** self.slowdown_exp)

    # ─── FIX-8: RAMP DUTY POSITION-FOLLOWING ─────────────────────────────────
    def _ramp_duty(self, cur_temp, target_now, seg_target_final, ramp_rate, measured_rate):
        """
        Control bazat pe POZITIA PE LINIA RAMPEI.
        error = target_now - cur_temp:
          negativ (cur > target_now) = suntem INAINTEA rampei → SSR OFF
          mic pozitiv (0-1C)         = pe linie → micro-duty
          mare pozitiv (>1C)         = in urma  → proportional + comp
        """
        error = target_now - cur_temp

        # ZONA 1: inaintea rampei → OFF
        if error < -self.ramp_ahead_band:
            self.compensation_factor = 0.0
            self.last_base_duty = 0.0
            return 0.0, 0.0

        # ZONA 2: pe linie → micro-duty
        if error <= self.ramp_hold_band:
            t = max(0.0, error) / self.ramp_hold_band
            duty = self.ramp_micro_min + t * (self.ramp_micro_max - self.ramp_micro_min)
            duty *= self._pre_hold_factor(cur_temp, seg_target_final)
            self.compensation_factor = 0.0
            self.last_base_duty = duty
            return max(0.0, duty), duty

        # ZONA 3: in urma → proportional + compensatie
        comp = self._adaptive_compensation(measured_rate, ramp_rate)
        self.compensation_factor = comp
        duty = self.ramp_base_duty + error * self.ramp_prop_gain
        duty *= (1.0 + comp)
        duty *= self._pre_hold_factor(cur_temp, seg_target_final)
        max_d = (self.cycle_time - self.min_ssr_off) / self.cycle_time
        duty = min(max_d, max(0.15, duty))
        self.last_base_duty = self.ramp_base_duty
        return duty, self.ramp_base_duty

    # ─── FIX-4: HOLD DUTY PROPORTIONAL ───────────────────────────────────────
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

    # ─── FIX-9: FRACTURE PROTECTION ──────────────────────────────────────────
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
            self.seg_temp_max = cur_temp
            return 'FRACTURE_RISK_DROP_' + str(round(drop, 1)) + 'C'
        return ''

    # ─── TARGET RAMP LINIAR ───────────────────────────────────────────────────
    def _ramp_target_now(self):
        """Pozitia ideala pe linia rampei la momentul curent."""
        if self.ramp_start_time is None or self.ramp_start_temp is None:
            return PROFILE[self.seg_idx]['target']
        elapsed_min = (time.time() - self.ramp_start_time) / 60.0
        tgt = self.ramp_start_temp + self.ramp_rate_fixed * elapsed_min
        lo  = min(self.ramp_start_temp, self.ramp_target_fixed)
        return min(self.ramp_target_fixed, max(lo, tgt))

    # ─── TRANZITII ────────────────────────────────────────────────────────────
    def _start_segment(self, cur_temp):
        """
        [FIX-10] La resume mid-ramp:
        - Rata ORIGINALA a segmentului (span_total / duration_min)
        - ramp_start_temp = originea segmentului (target seg anterior)
        - ramp_start_time = now - elapsed_deja (BACKTRACK)
        → target_now la boot = cur_temp, SegRem corect
        """
        if self.seg_idx >= len(PROFILE):
            self.program_complete = True
            return

        seg = PROFILE[self.seg_idx]

        if seg['type'] == 'ramp':
            if cur_temp >= seg['target'] - RESUME_MARGIN:
                print('  SKIP Seg' + str(seg['id']) + ': T=' +
                      str(round(cur_temp, 1)) + 'C >= target ' + str(seg['target']) + 'C')
                self._advance_segment(cur_temp)
                return

            # ── FIX-10: Calcul rata ORIGINALA + backtrack timp ────────────────
            boot_t  = self.resume_info['boot_temp']
            origin  = _seg_origin_temp(self.seg_idx, boot_t)
            span_total = seg['target'] - origin   # span COMPLET al segmentului

            if span_total > 0:
                original_rate = span_total / seg['duration_min']   # rata ORIGINALA
            else:
                original_rate = 0.001

            # Elapsed deja pe aceasta rampa (inainte de boot/resume)
            span_done = cur_temp - origin
            if span_done > 0 and original_rate > 0.001:
                elapsed_already_s = (span_done / original_rate) * 60.0
            else:
                elapsed_already_s = 0.0

            self.ramp_rate_fixed   = original_rate          # ORIGINAL, nu rezidual
            self.ramp_start_temp   = origin                  # originea segmentului
            self.ramp_target_fixed = seg['target']
            self.ramp_start_time   = time.time() - elapsed_already_s  # BACKTRACK
            self.seg_mode          = 'ramp'
            self.seg_temp_max      = cur_temp

            remaining_min = (seg['target'] - cur_temp) / original_rate
            elapsed_min   = (cur_temp - origin) / original_rate

            resume_tag = ' [RESUME]' if self.resume_info['mode'] != 'startup_fresh' else ''
            print('\n🚀 SEG' + str(seg['id']) + ' RAMP' + resume_tag +
                  ' ' + str(round(cur_temp, 1)) + 'C → ' + str(seg['target']) + 'C' +
                  ' | Rata: ' + str(round(original_rate, 2)) + 'C/min' +
                  ' | Deja: ' + str(round(elapsed_min, 1)) + 'min' +
                  ' | Ramas: ' + str(round(remaining_min, 1)) + 'min')
        else:
            self._enter_hold(cur_temp)

    def _enter_hold(self, cur_temp):
        seg = PROFILE[self.seg_idx]
        self.seg_mode        = 'hold'
        self.hold_start_time = time.time()
        self.seg_temp_max    = cur_temp
        self.comp_history.clear()
        self.compensation_factor = 0.0
        resume_tag = ' [RESUME]' if self.resume_info['mode'] != 'startup_fresh' else ''
        print('\n🎯 SEG' + str(seg['id']) + ' HOLD' + resume_tag +
              ' ' + str(seg['target']) + 'C | ' +
              str(seg['duration_min']) + 'min | T=' + str(round(cur_temp, 2)) + 'C')

    def _advance_segment(self, cur_temp):
        done = PROFILE[self.seg_idx]
        print('\n✅ SEG' + str(done['id']) + ' COMPLET | T=' + str(round(cur_temp, 2)) + 'C')
        self.seg_idx += 1
        if self.seg_idx >= len(PROFILE):
            self.program_complete = True
            print('\n🏁 PROGRAM COMPLET — SSR OFF')
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
            # FIX-2: tranzitie pe temperatura REALA
            if cur_temp >= seg['target'] - RESUME_MARGIN:
                self._enter_hold(cur_temp)
            return
        if self.seg_mode == 'hold':
            elapsed = (time.time() - self.hold_start_time) / 60.0
            if max(0.0, seg['duration_min'] - elapsed) <= 0.05:
                self._advance_segment(cur_temp)

    # ─── TIMING ───────────────────────────────────────────────────────────────
    def _seg_timing(self):
        seg = PROFILE[min(self.seg_idx, len(PROFILE) - 1)]
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

    # ─── UPDATE CICLU PRINCIPAL ───────────────────────────────────────────────
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

        seg = PROFILE[min(self.seg_idx, len(PROFILE) - 1)]

        # FIX-9: fracture check
        alarm = self._check_fracture(cur_temp, self.seg_mode)
        if alarm:
            self.ssr.value(0)
            print('\n⚠️  ' + alarm + ' | T=' + str(round(cur_temp, 2)) +
                  'C | SSR OFF | Seg' + str(seg['id']))

        # Calcul duty
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
                duty = 0.0
            on_time = duty * self.cycle_time

        elif self.seg_mode == 'hold':
            target = seg['target']
            duty, base_duty = self._hold_duty(cur_temp, seg['target'])
            self.last_base_duty = base_duty
            if alarm:
                duty = 0.0
            on_time = duty * self.cycle_time

        else:
            target, on_time, base_duty = seg['target'], 0.0, 0.0

        # FIX-5: enforce min SSR OFF
        if (now - self.last_ssr_off_time) < self.min_ssr_off:
            on_time = 0.0

        max_on   = self.cycle_time - self.min_ssr_off
        on_time  = min(on_time, max_on)
        off_time = max(self.min_ssr_off, self.cycle_time - on_time)
        error    = target - cur_temp

        prog_el, prog_rem = self._prog_timing()
        seg_el,  seg_rem  = self._seg_timing()

        # Actionare SSR
        ssr_state = False
        if on_time > 0.1 and not alarm:
            duty_pct = round((on_time / self.cycle_time) * 100.0, 0)
            cv = round(self.compensation_factor * 100.0, 1)
            cs = ('+' if cv >= 0 else '') + str(cv) + '%'
            print('🔥 SSR ON ' + str(round(on_time, 1)) + 's | T=' +
                  str(round(cur_temp, 2)) + 'C | ' + str(duty_pct) + '% [' + cs + '] | ' +
                  'Base=' + str(round(self.last_base_duty * 100.0, 0)) + '%')
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

        # Print comp la fiecare 5 min
        if prog_el - self.last_comp_print_min >= 5.0 and self.comp_history:
            avg_r = sum(self.comp_history) / len(self.comp_history)
            print('📊 COMP ' + str(round(self.compensation_factor * 100, 1)) +
                  '% | Rate_avg=' + str(round(avg_r, 2)) + 'C/min | Hist=' +
                  str(len(self.comp_history)))
            self.last_comp_print_min = prog_el

        # Print consola
        seg_label = 'S' + str(seg['id']) + '-' + self.seg_mode[:4].upper()
        cv = round(self.compensation_factor * 100, 1)
        cs = ('+' if cv >= 0 else '') + str(cv) + '%'
        alarm_tag = ' ⚠️  ' + alarm if alarm else ''

        print(rj(int(prog_el), 5) + 'm | ' +
              rj(round(cur_temp, 2), 7) + ' | ' +
              rj(round(target, 2), 7) + ' | ' +
              ('+' if error >= 0 else '') + rj(round(error, 2), 6) + ' | ' +
              ('+' if rate >= 0 else '') + rj(round(rate, 2), 4) + ' | ' +
              rj(round(on_time, 1), 4) + 's | ' +
              ('ON ' if ssr_state else 'OFF') + ' | ' +
              lj(seg_label, 8) + ' | ' +
              'SegRem:' + rj(round(seg_rem, 1), 5) + 'm | ' +
              'ProgRem:' + rj(round(prog_rem, 1), 6) + 'm | ' +
              rj(cs, 6) + alarm_tag)

        # Log CSV
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
        seg = PROFILE[min(self.seg_idx, len(PROFILE) - 1)]
        msg = ('\n🛑 STOP FORTAT | Seg' + str(seg['id']) + ' ' + self.seg_mode +
               ' | Elapsed: ' + str(round(prog_el, 1)) + 'min | Target: ' + str(seg['target']) + 'C')
        if self.fracture_alarm_count:
            msg = msg + ' | ALARME FRACTURA: ' + str(self.fracture_alarm_count)
        print(msg)


# ─── ORCHESTRATOR ─────────────────────────────────────────────────────────────
class FiringController:

    def __init__(self):
        self.kiln = CeramicKiln()
        self.kiln.begin()
        self._print_header()

    def _print_header(self):
        print('\n' + '=' * 110)
        print('   MIN | Tcur    | Ttgt    |  ERR    | RATE  | ON    | SSR'
              ' | SEG      | SegRem   | ProgRem  | COMP')
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
                          str(round(prog_el / 60.0, 2)) + ' ore')
                    if self.kiln.fracture_alarm_count:
                        print('  ⚠️  ALARME FRACTURA: ' + str(self.kiln.fracture_alarm_count))
                    print('=' * 110)
                    break
        except KeyboardInterrupt:
            self.kiln.force_stop()


if __name__ == '__main__':
    FiringController().run()
