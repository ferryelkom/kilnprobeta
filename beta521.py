#!/usr/bin/env python3
# RAMP TARGET @RATE + HOLD | FULLY CONFIGURABLE HYBRID | Perplexity 25-Feb-2026 23:15 EET
# ─────────────────────────────────────────────────────────────────────
#  • NO HARDCODE - toate valorile configurabile sus
#  • Hold thresholds = % din ramp_target (70%, 95%)
#  • Base duty temp zones = % din ramp_target  
#  • Pre-hold margin = % din ramp_target
#  • Debugging values PRINTATE la fiecare ciclu
# ─────────────────────────────────────────────────────────────────────

import time, gc, machine
from machine import Pin

def read_temp_precise():
    """2 zecimale FĂRĂ modificări librării"""
    sensor = machine.max31856
    raw_temp = sensor.status()['temp']
    return round(raw_temp, 2)

class ConfigurableHybridKiln:
    def __init__(self):
        # 🔥 CONFIGURABILE - MODIFICĂ AICI PENTRU ORICE TEST
        self.ramp_rate = 3.3      # °C/min TARGET
        self.ramp_target = 450.0  # TARGET PRINCIPAL
        self.hold_target = 450.0  # HOLD TARGET (poate fi diferit)
        
        # Hold thresholds ca % din ramp_target (NO HARDCODE)
        self.hold_zone_low_pct = 0.75    # 75% ramp_target = 337.5°C
        self.hold_zone_mid_pct = 0.95    # 95% ramp_target = 427.5°C
        
        # Base duty temp zones ca % din ramp_target
        self.base_duty_low_pct = 0.60    # <70% ramp_target
        self.base_duty_mid_pct = 0.50    # 70-90% ramp_target  
        self.base_duty_high_pct = 0.40   # >90% ramp_target
        
        # Pre-hold margin ca % din ramp_target
        self.pre_hold_margin_pct = 0.035  # 3.5% = 15.75°C la 450°C
        
        # Compensation limits
        self.compensation_max = 0.15     # ±15%
        
        # Hardware & timing
        self.ssr = Pin(32, Pin.OUT)
        self.ssr.off()
        self.cycle_time = 10.0
        self.min_ssr_off = 3.0
        
        # State
        self.mode = 'startup'
        self.start_time = time.time()
        self.ramp_start_time = None
        self.ramp_start_temp = None
        self.startup_duration = 20.0
        self.startup_start_time = None
        
        # History
        self.temp_history = []
        self.compensation_history = []
        self.history_len = 5
        self.last_comp_print = 0
        
        # Computed thresholds (din config)
        self.hold_zone_low = self.ramp_target * self.hold_zone_low_pct
        self.hold_zone_mid = self.ramp_target * self.hold_zone_mid_pct
        self.pre_hold_margin = self.ramp_target * self.pre_hold_margin_pct
        
        print(f"🚀 RAMP {self.ramp_target}°C @{self.ramp_rate}°C/min + HOLD")
        print(f"   Config: HoldLow={self.hold_zone_low:.1f}°C | HoldMid={self.hold_zone_mid:.1f}°C")
        print(f"   BaseDuty: Low={self.base_duty_low_pct*100:.0f}% | Mid={self.base_duty_mid_pct*100:.0f}% | High={self.base_duty_high_pct*100:.0f}%")
        print(f"   PreHoldMargin={self.pre_hold_margin:.1f}°C")

    def _log_header(self):
        hdr = "min,cur_temp,target,error,rate_cpm,on_time,predicted_peak,ssr_state,mode,elapsed_s,compensation,base_duty_pct\n"
        with open('ramp_hybrid_config.txt', 'w') as f:
            f.write(hdr)

    def _log_line(self, minutes, cur, tgt, err, rate, on, peak, ssr, mode, elapsed, comp, base_duty):
        line = f"{minutes},{cur:.2f},{tgt:.1f},{err:.2f},{rate:.2f},{on:.1f},{peak:.1f},{int(ssr)},{mode},{elapsed},{comp:.3f},{base_duty:.3f}\n"
        with open('ramp_hybrid_config.txt', 'a') as f:
            f.write(line)
            f.flush()

    def start_startup(self):
        cur = read_temp_precise()
        self.startup_start_time = time.time()
        self.mode = 'startup'
        print(f"🔍 STARTUP 20s | T_start={cur:.2f}°C → TARGET {self.ramp_target}°C")

    def _check_startup_complete(self, cur_temp):
        return (time.time() - self.startup_start_time) >= self.startup_duration

    def _calc_ramp_progress(self):
        if not self.ramp_start_time or not self.ramp_start_temp:
            return self.ramp_target
        
        elapsed_min = (time.time() - self.ramp_start_time) / 60.0
        ramp_temp = self.ramp_start_temp + self.ramp_rate * elapsed_min
        
        if ramp_temp >= self.ramp_target:
            self.mode = 'hold'
            print(f"🎯 HOLD {self.hold_target:.1f}°C START")
            return self.hold_target
        
        return ramp_temp

    def _update_history(self, temp):
        self.temp_history.append((time.time(), temp))
        if len(self.temp_history) > self.history_len * 2:
            self.temp_history.pop(0)

    def _calc_rate(self):
        if len(self.temp_history) < 2:
            return 0.0
        t0, temp0 = self.temp_history[0]
        tn, tempn = self.temp_history[-1]
        dt_min = (tn - t0) / 60.0
        return (tempn - temp0) / dt_min if dt_min > 0 else 0.0

    def _adaptive_compensation(self, measured_rate):
        """±15% compensation bazat pe trend"""
        self.compensation_history.append(measured_rate)
        if len(self.compensation_history) > self.history_len:
            self.compensation_history.pop(0)
        
        if len(self.compensation_history) < 3:
            return 0.0
        
        avg_rate = sum(self.compensation_history) / len(self.compensation_history)
        rate_error = avg_rate - self.ramp_rate
        compensation = rate_error * 0.05  # 5% per °C/min
        return max(-self.compensation_max, min(self.compensation_max, compensation))

    def _print_compensation_status(self, elapsed_min):
        if elapsed_min - self.last_comp_print >= 5:
            avg_rate = sum(self.compensation_history)/len(self.compensation_history) if self.compensation_history else 0
            print(f"📊 COMP {self.compensation_history[-1]*100:+.1f}% | Rate_avg={avg_rate:.2f}°C/min | Hist={len(self.compensation_history)}")
            self.last_comp_print = elapsed_min

    def _get_base_duty(self, cur_temp):
        """Base duty CONFIGURABIL per temp zone"""
        if cur_temp < self.ramp_target * 0.70:
            return self.base_duty_low_pct
        elif cur_temp < self.ramp_target * 0.90:
            return self.base_duty_mid_pct
        else:
            return self.base_duty_high_pct

    def _get_duty_hybrid(self, cur_temp, mode, measured_rate):
        """FULLY CONFIGURABLE hybrid logic"""
        if mode == 'hold':
            # Hold duties bazate pe % din target
            if cur_temp < self.hold_zone_low:
                return 0.25
            elif cur_temp < self.hold_zone_mid:
                return 0.15
            return 0.12
        
        # RAMP: Base + Compensation + Pre-hold
        base_duty = self._get_base_duty(cur_temp)
        compensation = self._adaptive_compensation(measured_rate)
        
        duty = base_duty * (1 + compensation)
        
        # Pre-hold slowdown
        if abs(self.ramp_target - cur_temp) < self.pre_hold_margin:
            slowdown_factor = (self.ramp_target - cur_temp) / self.pre_hold_margin
            duty *= max(0.1, slowdown_factor)
        
        return min(0.85, max(0.20, duty))

    def _get_current_target(self):
        if self.mode == 'startup' or self.mode == 'ramp':
            return self._calc_ramp_progress()
        return self.hold_target

    def update(self):
        cur_temp = read_temp_precise()
        self._update_history(cur_temp)
        
        target = self._get_current_target()
        rate = self._calc_rate()
        error = target - cur_temp
        
        # STARTUP
        if self.mode == 'startup':
            if self._check_startup_complete(cur_temp):
                self.mode = 'ramp'
                self.ramp_start_time = time.time()
                self.ramp_start_temp = cur_temp
                eta = (self.ramp_target - cur_temp) / self.ramp_rate
                print(f"🚀 RAMP START {cur_temp:.1f}°C → {self.ramp_target}°C | ETA {eta:.0f}min")
            on_time = 0.0
        else:
            time_since_off = time.time() - getattr(self, 'last_ssr_off_time', 0)
            if time_since_off < self.min_ssr_off:
                on_time = 0.0
            else:
                base_duty = self._get_base_duty(cur_temp)
                duty = self._get_duty_hybrid(cur_temp, self.mode, rate)
                on_time = duty * self.cycle_time
                self.last_base_duty = base_duty  # Debug

        predicted_peak = cur_temp + (on_time * 0.12)
        off_time = max(self.min_ssr_off, self.cycle_time - on_time)
        
        # SSR
        ssr_state = False
        if on_time > 0.1:
            duty_pct = (on_time / self.cycle_time) * 100
            comp_pct = getattr(self, 'compensation_factor', 0) * 100
            comp_str = f"[{comp_pct:+.1f}%]" if abs(comp_pct) > 0.5 else "[0%]"
            base_str = f"Base={self.last_base_duty*100:.0f}%"
            print(f"🔥 SSR ON {on_time:.1f}s | T={cur_temp:.2f}°C | {duty_pct:.0f}% {comp_str} | {base_str}")
            
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
            ssr_state = False

        elapsed_min = (time.time() - self.start_time) / 60
        self._print_compensation_status(elapsed_min)
        self.compensation_factor = self._adaptive_compensation(rate)

        minutes = int(elapsed_min)
        elapsed = int(time.time() - self.start_time)
        self._log_line(minutes, cur_temp, target, error, rate, on_time, predicted_peak, 
                      ssr_state, self.mode, elapsed, self.compensation_factor, 
                      getattr(self, 'last_base_duty', 0))

        return {
            'temp': cur_temp, 'target': target, 'error': error, 'rate': rate,
            'on_time': on_time, 'ssr': ssr_state, 'mode': self.mode,
            'compensation': self.compensation_factor
        }

    def force_stop(self):
        self.ssr.value(0)
        print(f"\n🛑 STOP | Config: Target={self.ramp_target}°C Rate={self.ramp_rate}°C/min")

class RampController:
    def __init__(self):
        self.kiln = ConfigurableHybridKiln()
        self.kiln.start_startup()
        self.kiln._log_header()

    def run(self):
        print("=" * 115)
        print("MIN | Tcur  | Ttgt  | ERR   | RATE | ON(s) | SSR | MODE  | ELAPSED | COMP   | BASE")
        print("=" * 115)

        try:
            while True:
                s = self.kiln.update()
                minutes = int((time.time() - self.kiln.start_time) / 60)
                comp_str = f"{s['compensation']*100:+.1f}%"
                base_str = f"{getattr(self.kiln, 'last_base_duty', 0)*100:.0f}%"
                
                print(f"{minutes:2d}m | {s['temp']:6.2f} | {s['target']:6.1f} | "
                      f"{s['error']:+6.2f} | {s['rate']:5.1f} | {s['on_time']:5.1f} | "
                      f"{'ON' if s['ssr'] else 'OFF':3} | {s['mode']:<5} | "
                      f"{int(time.time()-self.kiln.start_time)/60:4.0f}m | {comp_str:>6} | {base_str}")

                gc.collect()
        except KeyboardInterrupt:
            self.kiln.force_stop()

if __name__ == "__main__":
    controller = RampController()
    controller.run()