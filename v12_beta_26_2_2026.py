#!/usr/bin/env python3
# SSR 12-SEGMENT BANG BANG | v12 beta 26/2/2026: HOLD LOOP FIXED + USER SELECT PROFILE
# ─────────────────────────────────────────────────────────────────────
# in curs de opimizare, stabil pana acum 


import time, gc, machine
from machine import Pin

def read_temp():
    sensor = machine.max31856
    return sensor.status()['temp']

class MultiSegmentBangBang:
    def __init__(self):
        self.ssr = Pin(32, Pin.OUT)
        self.ssr.off()

        self.segments = [
            {"start_temp": 0.0, "target": 150.0, "ramp_rate": 2.5, "hold_min": 0, "boost_pct": +2.5},  #netestat, boost netestat
            {"start_temp": 150.0, "target": 150.0, "ramp_rate": 0.0, "hold_min": 180, "boost_pct": +2.8}, #netestat, boost netestat
            {"start_temp": 150.0, "target": 370.0, "ramp_rate": 3.66, "hold_min": 0, "boost_pct": +3.5}, #netestat, boost netestat
            {"start_temp": 370.0, "target": 370.0, "ramp_rate": 0.0, "hold_min": 60, "boost_pct": +3.3}, #stabil cu abarere 3 grade
            {"start_temp": 370.0, "target": 448.0, "ramp_rate": 2.5, "hold_min": 0, "boost_pct": +4.4}, #boost 4.4 urca 2.4 pe interval 373-456 iar 2.1 pe interval 373-449
            {"start_temp": 448.0, "target": 523.0, "ramp_rate": 2.5, "hold_min": 0, "boost_pct": +5.4}, # test in curs
            {"start_temp": 523.0, "target": 700.0, "ramp_rate": 2.5, "hold_min": 0, "boost_pct": +6.4}, #urmeaza test
            {"start_temp": 700.0, "target": 750.0, "ramp_rate": 2.5, "hold_min": 240, "boost_pct": +7.0},
            {"start_temp": 750.0, "target": 640.0, "ramp_rate": 0.0, "hold_min": 0, "boost_pct": 0.0},
#            {"start_temp": 1250.0, "target": 1250.0, "ramp_rate": 0.0, "hold_min": 180, "boost_pct": 0.0},
#            {"start_temp": 1250.0, "target": 1100.0, "ramp_rate": -2.0, "hold_min": 60, "boost_pct": 0.0},
#            {"start_temp": 1100.0, "target": 800.0, "ramp_rate": -3.0, "hold_min": 120, "boost_pct": 0.0},
#            {"start_temp": 800.0, "target": 25.0, "ramp_rate": -5.0, "hold_min": 0, "boost_pct": 0.0}
        ]
        
        # State variables
        self.current_seg_idx = 0
        self.seg_start_time = None
        self.seg_start_temp = None
        self.seg_elapsed_min = 0
        self.profile_complete = False
        self.mode = 'ramp'
        self._hold_active = False  # 🔥 FIX5: Anti-hold loop flag!
        self.cycle_time = 10.0
        self.min_ssr_off = 2.0
        self.last_ssr_off_time = 0
        self.temp_history = []
        self.history_len = 5
        self.start_time = time.time()
        
        try:
            self.log_file = open('profile_run.txt', 'w')
            self._log_header()
        except:
            self.log_file = None

        print("✅ v12: - HOLD LOOP FIXED + USER SELECT!")
        self._print_profile()
        self._user_select_segment()

    def _print_profile(self):
        print("═" * 90)
        print("AVAILABLE SEGMENTS - SELECT START:")
        for i, seg in enumerate(self.segments):
            hold = f" Hold:{seg['hold_min']}m" if seg['hold_min'] > 0 else ""
            print(f"S{i+1:2d}: {seg['start_temp']:4.0f}→{seg['target']:5.0f}°C@{seg['ramp_rate']:5.2f}°C/min{hold}")
        print("═" * 90)

    def _user_select_segment(self):
        """USER SELECT: Care segment să pornească"""
        cur_temp = read_temp()
        print(f"\n🔍 Tcurentă={cur_temp:.1f}°C")
        print("\n📋 ALEGE SEGMENTUL DE START (1-13):")
        
        while True:
            try:
                choice = input("ENTER S# (ex: 3, 4, 5): ").strip()
                seg_idx = int(choice) - 1
                
                if 0 <= seg_idx < len(self.segments):
                    selected_seg = self.segments[seg_idx]
                    print(f"\n✅ START S{seg_idx+1}: {selected_seg['start_temp']:.0f}→{selected_seg['target']:.0f}°C")
                    print(f"   Ramp: {selected_seg['ramp_rate']:.1f}°C/min | Hold: {selected_seg['hold_min']}min")
                    
                    self.current_seg_idx = seg_idx
                    self.seg_start_time = time.time()
                    self.seg_start_temp = cur_temp
                    self._hold_active = False  # Reset hold flag
                    self.mode = 'ramp'
                    break
                else:
                    print(f"❌ Invalid! S1-S{len(self.segments)}")
            except:
                print("❌ Enter number 1-13!")

    def _log_header(self):
        if self.log_file:
            self.log_file.write("min,seg,cur_temp,target,error,rate,on_time,duty_pct,ssr,mode\n")
            self.log_file.flush()

    def _log_line(self, minutes, seg_idx, cur, tgt, err, rate, on_time, duty_pct, ssr, mode):
        if self.log_file:
            try:
                self.log_file.write(f"{minutes},{seg_idx},{cur:.1f},{tgt:.1f},{err:.1f},{rate:.2f},{on_time:.1f},{duty_pct:.1f},{int(ssr)},{mode}\n")
                self.log_file.flush()
            except: pass

    def _get_current_segment(self):
        if self.seg_start_time:
            self.seg_elapsed_min = (time.time() - self.seg_start_time) / 60.0
        return self.current_seg_idx, self.seg_elapsed_min

    def _current_setpoint(self):
        """FIX1+FIX2: FIXED seg_start_temp per segment"""
        seg_idx, _ = self._get_current_segment()
        seg = self.segments[seg_idx]
        
        if self.seg_start_temp is None:
            self.seg_start_temp = read_temp()
        
        start_temp = self.seg_start_temp
        delta_t = seg['target'] - start_temp
        
        if seg['ramp_rate'] == 0 or delta_t <= 0:
            return seg['target']
        
        time_elapsed = (time.time() - self.seg_start_time) if self.seg_start_time else 0
        ramp_time_sec = abs(delta_t / seg['ramp_rate']) * 60
        progress = min(1.0, time_elapsed / ramp_time_sec) if ramp_time_sec > 0 else 0
        
        setpoint = start_temp + progress * delta_t
        return setpoint

    def _update_history(self, temp):
        now = time.time()
        self.temp_history.append((now, temp))
        if len(self.temp_history) > self.history_len:
            self.temp_history.pop(0)

    def _calc_rate(self):
        if len(self.temp_history) < 2: return 0.0
        t0, t0_temp = self.temp_history[0]
        tn, tn_temp = self.temp_history[-1]
        dt_min = (tn - t0) / 60.0
        return (tn_temp - t0_temp) / dt_min if dt_min > 0 else 0.0

    def _determine_on_time(self, cur_temp, target, rate, seg):
        """FIX3+FIX4: HOLD prioritate + rate_error corect"""
        time_since_off = time.time() - self.last_ssr_off_time
        if time_since_off < self.min_ssr_off:
            return 0.0, 0.0

        error = target - cur_temp
        required_rate = abs(seg['ramp_rate'])
        boost = seg['boost_pct']

        # 🔥 FIX4: HOLD prioritate absolută!
        if self.mode == 'hold':
            if error > 0:
                duty_pct = 12 + error * 0.4
                duty_pct = max(8, min(45, duty_pct))
            else:
                duty_pct = 0
        elif seg['ramp_rate'] > 0:
            rate_error = required_rate - rate  # FIX3
            base_duty = required_rate * 3
            duty_pct = base_duty + boost * 6 + rate_error * 2
            duty_pct = max(25, min(95, duty_pct))
        elif seg['ramp_rate'] == 0 and self.mode != 'hold':
            if error > 0:
                duty_pct = 12 + error * 0.3
                duty_pct = max(10, min(35, duty_pct))
            else:
                duty_pct = 0
        else:
            duty_pct = 0

        on_time = (duty_pct / 100.0) * self.cycle_time
        return on_time, duty_pct

    def _check_segment_complete(self, cur_temp):
        """🔥 FIX5: Hold declanșat o SINGURĂ dată per segment!"""
        seg_idx, elapsed_min = self._get_current_segment()
        seg = self.segments[seg_idx]
        
        target_reached = abs(cur_temp - seg['target']) <= 3.0
        
        # 🔥 FIX5.1: Hold declanșat DOAR dacă NU e deja activat
        if seg['hold_min'] > 0 and target_reached and not self._hold_active:
            print(f"\n🔥 HOLD S{seg_idx+1}: {cur_temp:.1f}°C x{seg['hold_min']}min")
            self._hold_active = True  # FLAG anti-loop
            self.mode = 'hold'
            self.seg_start_time = time.time()  # Countdown pornit
            return False
        
        # 🔥 FIX5.2: Verifică hold timer (doar dacă activ)
        if seg['hold_min'] > 0 and self._hold_active and self.mode == 'hold':
            if elapsed_min >= seg['hold_min']:
                print(f"\n✅ HOLD COMPLETE S{seg_idx+1}")
                self._hold_active = False  # Reset pentru următorul segment
                return True
            return False
        
        # Ramp OK (fără hold sau hold completat)
        if target_reached and seg['hold_min'] == 0:
            print(f"\n✅ RAMP OK S{seg_idx+1}: {cur_temp:.1f}°C")
            return True
        
        return False

    def update(self):
        cur_temp = read_temp()
        self._update_history(cur_temp)

        seg_idx, seg_elapsed = self._get_current_segment()
        seg = self.segments[seg_idx]
        target = self._current_setpoint()
        rate = self._calc_rate()

        if self._check_segment_complete(cur_temp):
            self.current_seg_idx += 1
            if self.current_seg_idx >= len(self.segments):
                self.profile_complete = True
                return {'complete': True}
            
            self.seg_start_time = time.time()
            self.seg_start_temp = None
            self._hold_active = False  # Reset hold flag pentru noul segment
            next_seg = self.segments[self.current_seg_idx]
            self.mode = 'ramp'
            print(f"\n➤ NEXT S{self.current_seg_idx+1}: {next_seg['start_temp']:.0f}→{next_seg['target']:.0f}°C")

        return self._safe_cycle(cur_temp, target, rate, seg_idx, seg_elapsed, seg)

    def _safe_cycle(self, cur_temp, target, rate, seg_idx, seg_elapsed, seg):
        try:
            on_time, duty_pct = self._determine_on_time(cur_temp, target, rate, seg)
            error = target - cur_temp

            ssr_state = False
            if on_time > 0.1:
                self.ssr.value(1)
                time.sleep(on_time)
                self.ssr.value(0)
                self.last_ssr_off_time = time.time()
                time.sleep(max(self.min_ssr_off, self.cycle_time - on_time))
                ssr_state = True
            else:
                self.ssr.value(0)
                self.last_ssr_off_time = time.time()
                time.sleep(self.cycle_time)

            minutes = int((time.time() - self.start_time) / 60)
            self._log_line(minutes, seg_idx, cur_temp, target, error, rate, on_time, duty_pct, ssr_state, self.mode)

            return {
                'temp': round(cur_temp, 1), 'target': round(target, 1), 'error': round(error, 1),
                'rate': round(rate, 2), 'on_time': round(on_time, 2), 'duty_pct': round(duty_pct, 1),
                'ssr': ssr_state, 'mode': self.mode, 'seg': seg_idx + 1, 'boost': round(seg['boost_pct'], 1),
                'seg_time': round(seg_elapsed, 1)
            }
        except Exception as e:
            print(f"⚠ ERROR: {e}")
            self.ssr.value(0)
            time.sleep(self.cycle_time)
            return {
                'temp': round(cur_temp, 1), 'target': round(cur_temp, 1), 'error': 0,
                'rate': 0, 'on_time': 0, 'duty_pct': 0, 'ssr': False, 'mode': self.mode,
                'seg': seg_idx + 1, 'boost': 0, 'seg_time': round(seg_elapsed, 1)
            }

    def force_stop(self):
        self.ssr.value(0)
        if self.log_file:
            try:
                self.log_file.close()
                print("✅ LOG SAVED: profile_run.txt")
            except: pass
        print("🛑 EMERGENCY STOP")

class ProfileRunController:
    def __init__(self):
        self.bc = MultiSegmentBangBang()

    def run(self):
        print("\n═" * 150)
        print("MIN |  S# |  Tcur | Ttgt  |  ERR |  RATE | ON(s)| DUTY%  | SSR | MODE  | BOOST | SEG_TIME")
        print("═" * 150)

        try:
            while True:
                s = self.bc.update()
                if self.bc.profile_complete:
                    print("\n🎉 SELECTED SEGMENT(S) COMPLETE!")
                    break
                
                minutes = int((time.time() - self.bc.start_time) / 60)
                print(f"{minutes:2d}m | S{s['seg']:2d} | {s['temp']:5.1f} | {s['target']:5.1f} | "
                      f"{s['error']:+4.1f} | {s['rate']:5.2f} | {s['on_time']:4.1f} | "
                      f"{s['duty_pct']:5.1f}% | {'ON' if s['ssr'] else 'OFF':>3} | "
                      f"{s['mode']:<5} | {s['boost']:+4.1f}% | {s['seg_time']:5.1f}m")
                gc.collect()
        except KeyboardInterrupt:
            self.bc.force_stop()

if __name__ == "__main__":
    rc = ProfileRunController()
    rc.run()
#de optimizat logic, determinat valori boost optimale. pana acum cea mai buna versiune functionala
