#Micropython kiln
# SSR 12-SEGMENT BANG BANG | betav13o2-FIXED: Eliminat smooth transition ramp->ramp
# hardware esp32e E32R35T
# SSR-fotek 40da
# max31856 adafruit + thermocouple R type 
# STRATEGY: Respect thermal mass, cap duty per segment, reduce power as approaching target, prevent overshoot
# FIX: Smooth transition DOAR pentru ramp->hold, NU pentru ramp->ramp

import time, gc, machine
from machine import Pin

def read_temp():
    """Read temperature from MAX31856 thermocouple"""
    try:
        sensor = machine.max31856
        return sensor.status()['temp']
    except:
        return 25.0

class MultiSegmentBangBang:
    def __init__(self):
        self.ssr = Pin(32, Pin.OUT)
        self.ssr.off()

        self.segments = [
            {"start_temp": 0.0, "target": 150.0, "ramp_rate": 2.5, "hold_min": 0, "boost_pct": +0.0},
            {"start_temp": 150.0, "target": 150.0, "ramp_rate": 0.0, "hold_min": 180, "boost_pct": +1.0},
            {"start_temp": 150.0, "target": 370.0, "ramp_rate": 3.66, "hold_min": 0, "boost_pct": +1.0},
            {"start_temp": 370.0, "target": 370.0, "ramp_rate": 0.0, "hold_min": 60, "boost_pct": +4.0},
            {"start_temp": 370.0, "target": 448.0, "ramp_rate": 2.5, "hold_min": 0, "boost_pct": +4.4},
            {"start_temp": 448.0, "target": 523.0, "ramp_rate": 2.5, "hold_min": 0, "boost_pct": +5.4},
            {"start_temp": 523.0, "target": 635.0, "ramp_rate": 2.5, "hold_min": 0, "boost_pct": +6.4},
            {"start_temp": 635.0, "target": 750.0, "ramp_rate": 2.5, "hold_min": 0, "boost_pct": +8.4},
            {"start_temp": 750.0, "target": 750.0, "ramp_rate": 0.0, "hold_min": 240, "boost_pct": 8.0},
        ]
        
        # Per-segment duty limits (thermal mass aware)
        self.DUTY_LIMITS = {
            0: 20,   # S1 (150°C)
            1: 11.5, # S2 hold
            2: 45,   # S3 (370°C)
            3: 45,   # S4 hold
            4: 62,   # S5 (448°C)
            5: 62,   # S6 (523°C)
            6: 68,   # S7 (635°C)
            7: 72,   # S8 (750°C)
            8: 40,   # S9 hold
        }
        
        # State variables
        self.current_seg_idx = 0
        self.seg_start_time = None
        self.seg_start_temp = None
        self.seg_elapsed_min = 0
        self.profile_complete = False
        self.mode = 'ramp'
        self._hold_active = False
        self.ssr_state = False
        
        # Control parameters
        self.cycle_time = 10.0
        self.min_ssr_off = 2.0
        self.last_ssr_off_time = 0
        
        # Deadband & Hysteresis for hold mode
        self.hold_deadband = 2.0
        self.hold_hysteresis = 1.5        
        
        # Temperature filtering
        self.filtered_temp = None
        self.filter_alpha = 0.3
        
        # Rate smoothing
        self.filtered_rate = 0.0
        self.rate_alpha = 0.4
        
        self.temp_history = []
        self.history_len = 8
        self.start_time = time.time()
        
        try:
            self.log_file = open('profile_run.txt', 'w')
            self._log_header()
        except:
            self.log_file = None

        print("✅ betav13o2-FIXED: Eliminat smooth transition ramp->ramp")
        print("   Smooth transition doar pentru ramp->hold")
        self._print_profile()
        self._user_select_segment()

    def _print_profile(self):
        print("═" * 100)
        print("AVAILABLE SEGMENTS - SELECT START:")
        for i, seg in enumerate(self.segments):
            hold = f" Hold:{seg['hold_min']}m" if seg['hold_min'] > 0 else ""
            print(f"S{i+1:2d}: {seg['start_temp']:4.0f}→{seg['target']:5.0f}°C@{seg['ramp_rate']:5.2f}°C/min{hold}")
        print("═" * 100)

    def _user_select_segment(self):
        """USER SELECT: Care segment să pornească"""
        cur_temp = read_temp()
        print(f"\n🔍 Tcurentă={cur_temp:.1f}°C")
        print("\n📋 ALEGE SEGMENTUL DE START (1-9):")
        
        while True:
            try:
                choice = input("ENTER S# (ex: 1, 2, 3): ").strip()
                seg_idx = int(choice) - 1
                
                if 0 <= seg_idx < len(self.segments):
                    selected_seg = self.segments[seg_idx]
                    print(f"\n✅ START S{seg_idx+1}: {selected_seg['start_temp']:.0f}→{selected_seg['target']:.0f}°C")
                    print(f"   Ramp: {selected_seg['ramp_rate']:.1f}°C/min | Hold: {selected_seg['hold_min']}min")
                    print(f"   Duty limit: {self.DUTY_LIMITS[seg_idx]}% (thermal mass safe)")
                    
                    self.current_seg_idx = seg_idx
                    self.seg_start_time = time.time()
                    self.seg_start_temp = cur_temp
                    self.filtered_temp = cur_temp
                    self._hold_active = False
                    self.ssr_state = False
                    self.mode = 'ramp'
                    break
                else:
                    print(f"❌ Invalid! S1-S{len(self.segments)}")
            except:
                print("❌ Enter number 1-9!")

    def _log_header(self):
        if self.log_file:
            self.log_file.write("min,seg,raw_temp,filt_temp,target,error,rate,filt_rate,on_time,duty_pct,duty_limit,duty_reduced,ssr,mode\n")
            self.log_file.flush()

    def _log_line(self, minutes, seg_idx, raw, filt, tgt, err, rate, filt_rate, on_time, duty_pct, duty_limit, duty_reduced, ssr, mode):
        if self.log_file:
            try:
                self.log_file.write(f"{minutes},{seg_idx},{raw:.1f},{filt:.1f},{tgt:.1f},{err:.1f},{rate:.2f},{filt_rate:.2f},{on_time:.1f},{duty_pct:.1f},{duty_limit:.1f},{duty_reduced:.1f},{int(ssr)},{mode}\n")
                self.log_file.flush()
            except: pass

    def _get_current_segment(self):
        if self.seg_start_time:
            self.seg_elapsed_min = (time.time() - self.seg_start_time) / 60.0
        return self.current_seg_idx, self.seg_elapsed_min

    def _apply_temp_filter(self, raw_temp):
        """Exponential moving average filter"""
        if self.filtered_temp is None:
            self.filtered_temp = raw_temp
        
        self.filtered_temp = self.filter_alpha * raw_temp + (1 - self.filter_alpha) * self.filtered_temp
        return self.filtered_temp
    
    def _apply_rate_filter(self, raw_rate):
        """Rate smoothing"""
        self.filtered_rate = self.rate_alpha * raw_rate + (1 - self.rate_alpha) * self.filtered_rate
        return self.filtered_rate

    def _get_duty_reduction_multiplier(self, error):
        """
        Reduce duty proportionally as error decreases.
        Prevents energy accumulation in thermal mass.
        """
        abs_error = abs(error)
        
        if abs_error > 15:
            return 1.0      # Full duty allowed (large error)
        elif abs_error > 10:
            return 0.85     # 15% reduction
        elif abs_error > 5:
            return 0.65     # 35% reduction
        elif abs_error > 2:
            return 0.40     # 60% reduction
        else:
            return 0.15     # 85% reduction (approaching target)

    def _current_setpoint(self):
        """Setpoint ramp with lead-time compensation"""
        seg_idx, elapsed_min = self._get_current_segment()
        seg = self.segments[seg_idx]
        
        if self.seg_start_temp is None:
            self.seg_start_temp = self.filtered_temp if self.filtered_temp else read_temp()
        
        start_temp = self.seg_start_temp
        target = seg['target']
        ramp_rate = abs(seg['ramp_rate'])
        
        if ramp_rate == 0 or target <= start_temp:
            return target
        
        # Lead-time compensation
        ideal_temp = start_temp + (ramp_rate * elapsed_min)
        
        if elapsed_min < 5:
            lead_factor = 1.15
        else:
            lead_factor = 1.10
        
        lead_temp = start_temp + (ramp_rate * elapsed_min * lead_factor)
        setpoint = min(lead_temp, target)
        
        return setpoint

    def _update_history(self, raw_temp):
        now = time.time()
        filtered = self._apply_temp_filter(raw_temp)
        
        self.temp_history.append((now, filtered))
        if len(self.temp_history) > self.history_len:
            self.temp_history.pop(0)

    def _calc_rate(self):
        """Calculate heating rate with exponential smoothing"""
        if len(self.temp_history) < 2:
            return 0.0
        
        t0, t0_temp = self.temp_history[0]
        tn, tn_temp = self.temp_history[-1]
        dt_min = (tn - t0) / 60.0
        
        if dt_min <= 0.01:
            return 0.0
        
        raw_rate = (tn_temp - t0_temp) / dt_min
        filtered = self._apply_rate_filter(raw_rate)
        
        return filtered

    def _get_adaptive_cycle_time(self, rate, error):
        """Adaptively adjust cycle time"""
        cycle = 10.0
        
        if abs(rate) < 0.8:
            cycle = 12.0
        
        if abs(error) > 30.0:
            cycle = 7.0
        elif abs(error) > 15.0:
            cycle = 8.0
        
        return cycle

    def _determine_on_time(self, cur_temp, target, rate, seg):
        """Bang-bang duty calculation with error-based reduction"""
        seg_idx, _ = self._get_current_segment()
        duty_limit = self.DUTY_LIMITS.get(seg_idx, 60)
        
        error = target - cur_temp
        boost = seg['boost_pct']
        required_rate = abs(seg['ramp_rate'])
        
        duty_pct = 0
        duty_reduced = 0
        
        # HOLD MODE
        if self.mode == 'hold':
            if abs(error) < self.hold_deadband:
                duty_pct = 0
            elif error > self.hold_hysteresis:
                duty_pct = 15 + error * 1.5
                duty_pct = max(10, min(35, duty_pct))
            else:
                duty_pct = 0
            
            duty_pct = min(duty_pct, duty_limit)
            duty_reduced = duty_pct
        
        # RAMP MODE with error-based reduction
        elif seg['ramp_rate'] > 0:
            rate_error = required_rate - rate
            
            # Base duty (conservative, no aggressive catch-up)
            if rate_error > 2.0:
                base_duty = 55
            elif rate_error > 0:
                base_duty = required_rate * 3.5
            else:
                base_duty = required_rate * 3.0
            
            duty_pct = base_duty + boost * 6 + rate_error * 1.0
            duty_pct = max(25, min(95, duty_pct))
            
            # Step 1: Cap at segment-specific safe limit
            duty_pct = min(duty_pct, duty_limit)
            
            # Step 2: Apply error-based reduction (CRITICAL!)
            reduction_multiplier = self._get_duty_reduction_multiplier(error)
            duty_reduced = duty_pct * reduction_multiplier
            duty_reduced = max(15, duty_reduced)
            
            duty_pct = duty_reduced
        
        # MAINTAIN MODE
        elif seg['ramp_rate'] == 0 and self.mode != 'hold':
            if error > 0:
                duty_pct = 15 + error * 0.3
                duty_pct = max(10, min(40, duty_pct))
            else:
                duty_pct = 0
            
            duty_pct = min(duty_pct, duty_limit)
            duty_reduced = duty_pct
        else:
            duty_pct = 0
            duty_reduced = 0

        on_time = (duty_pct / 100.0) * self.cycle_time
        return on_time, duty_pct, duty_limit, duty_reduced

    def _check_segment_complete(self, cur_temp):
        """Check if segment complete"""
        seg_idx, elapsed_min = self._get_current_segment()
        seg = self.segments[seg_idx]
        
        target_reached = abs(cur_temp - seg['target']) <= 3.0
        
        if seg['hold_min'] > 0 and target_reached and not self._hold_active:
            print(f"\n🔥 HOLD S{seg_idx+1}: {cur_temp:.1f}°C x{seg['hold_min']}min")
            self._hold_active = True
            self.ssr_state = False
            self.mode = 'hold'
            self.seg_start_time = time.time()
            return False
        
        if seg['hold_min'] > 0 and self._hold_active and self.mode == 'hold':
            if elapsed_min >= seg['hold_min']:
                print(f"\n✅ HOLD COMPLETE S{seg_idx+1}")
                self._hold_active = False
                return True
            return False
        
        if target_reached and seg['hold_min'] == 0:
            print(f"\n✅ RAMP OK S{seg_idx+1}: {cur_temp:.1f}°C")
            return True
        
        return False

    def update(self):
        raw_temp = read_temp()
        self._update_history(raw_temp)

        seg_idx, seg_elapsed = self._get_current_segment()
        seg = self.segments[seg_idx]
        target = self._current_setpoint()
        rate = self._calc_rate()

        if self._check_segment_complete(self.filtered_temp):
            self.current_seg_idx += 1
            if self.current_seg_idx >= len(self.segments):
                self.profile_complete = True
                return {'complete': True}
            
            self.seg_start_time = time.time()
            
            # 🔥 FIX PRINCIPAL: Smooth transition DOAR pentru ramp->hold
            next_seg = self.segments[self.current_seg_idx]
            prev_seg = self.segments[self.current_seg_idx - 1]
            
            # Dacă next segment este HOLD (ramp_rate == 0), folosim smooth transition
            if next_seg['ramp_rate'] == 0:
                self.seg_start_temp = None  # Va folosi temp curentă
                print(f"   → Smooth transition către HOLD")
            # Dacă next segment este RAMP, continua direct de la target anterior
            elif next_seg['ramp_rate'] > 0:
                self.seg_start_temp = prev_seg['target']  # Continue de la target anterior
                print(f"   → Direct ramp către {next_seg['target']:.0f}°C")
            else:
                self.seg_start_temp = None
            
            self._hold_active = False
            self.ssr_state = False
            self.mode = 'ramp'
            print(f"\n➤ NEXT S{self.current_seg_idx+1}: {next_seg['start_temp']:.0f}→{next_seg['target']:.0f}°C")

        return self._safe_cycle(raw_temp, self.filtered_temp, target, rate, seg_idx, seg_elapsed, seg)

    def _safe_cycle(self, raw_temp, filt_temp, target, rate, seg_idx, seg_elapsed, seg):
        try:
            on_time, duty_pct, duty_limit, duty_reduced = self._determine_on_time(filt_temp, target, rate, seg)
            error = target - filt_temp
            
            cycle_time = self._get_adaptive_cycle_time(rate, error)

            ssr_state = False
            if on_time > 0.1:
                self.ssr.value(1)
                time.sleep(on_time)
                self.ssr.value(0)
                self.last_ssr_off_time = time.time()
                time.sleep(max(self.min_ssr_off, cycle_time - on_time))
                ssr_state = True
            else:
                self.ssr.value(0)
                self.last_ssr_off_time = time.time()
                time.sleep(cycle_time)

            minutes = int((time.time() - self.start_time) / 60)
            self._log_line(minutes, seg_idx, raw_temp, filt_temp, target, error, rate, self.filtered_rate, on_time, duty_pct, duty_limit, duty_reduced, ssr_state, self.mode)

            return {
                'temp': round(filt_temp, 1), 'raw_temp': round(raw_temp, 1), 'target': round(target, 1), 
                'error': round(error, 1), 'rate': round(rate, 2), 'on_time': round(on_time, 2), 
                'duty_pct': round(duty_pct, 1), 'duty_limit': round(duty_limit, 1), 'duty_reduced': round(duty_reduced, 1),
                'ssr': ssr_state, 'mode': self.mode, 'seg': seg_idx + 1, 
                'boost': round(seg['boost_pct'], 1), 'seg_time': round(seg_elapsed, 1)
            }
        except Exception as e:
            print(f"⚠ ERROR: {e}")
            self.ssr.value(0)
            time.sleep(self.cycle_time)
            return {
                'temp': round(filt_temp, 1), 'raw_temp': round(raw_temp, 1), 'target': round(filt_temp, 1), 
                'error': 0, 'rate': 0, 'on_time': 0, 'duty_pct': 0, 'duty_limit': 0, 'duty_reduced': 0,
                'ssr': False, 'mode': self.mode,
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
        print("\n" + "═" * 200)
        print("MIN | S# | Raw°C | Filt° | Tgt° | ERR | RATE | ON(s)| DUTY%| LIMIT| REDUC| SSR | MODE |")
        print("═" * 200)

        try:
            while True:
                s = self.bc.update()
                if self.bc.profile_complete:
                    print("\n🎉 PROFILE COMPLETE!")
                    break
                
                minutes = int((time.time() - self.bc.start_time) / 60)
                print(f"{minutes:2d}m | S{s['seg']:1d} | {s['raw_temp']:5.1f} | {s['temp']:5.1f} | {s['target']:5.1f} | "
                      f"{s['error']:+4.1f} | {s['rate']:5.2f} | {s['on_time']:4.1f} | "
                      f"{s['duty_pct']:5.1f}% | {s['duty_limit']:5.1f}% | {s['duty_reduced']:5.1f}% | "
                      f"{'ON' if s['ssr'] else 'OF':>2} | {s['mode']:<4} |")
                gc.collect()
        except KeyboardInterrupt:
            self.bc.force_stop()

if __name__ == "__main__":
    rc = ProfileRunController()
    rc.run()

# betav13o2-FIXED CHANGELOG:
# 🔥 ELIMINAT smooth transition de la ramp la alt ramp
# ✅ Smooth transition DOAR pentru ramp -> hold
# ✅ La tranziția ramp->ramp: seg_start_temp = prev_seg['target'] (continuă direct)
# ✅ La tranziția ramp->hold: seg_start_temp = None (smooth transition)
# ✅ Păstrat tot restul: duty limits, error reduction, thermal mass aware
