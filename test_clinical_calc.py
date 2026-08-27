"""
Verification script for clinical_calc.py -- checks every formula against
hand-computed or well-known reference values. Not a pytest suite (no
network/library assumptions); run directly with `python3 test_clinical_calc.py`.
"""
import math
import clinical_calc as cc

failures = []


def check(name, actual, expected, tol=0.05):
    ok = abs(actual - expected) <= tol
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: got {actual:.4f}, expected ~{expected:.4f} (tol {tol})")
    if not ok:
        failures.append(name)


def check_eq(name, actual, expected):
    ok = actual == expected
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: got {actual!r}, expected {expected!r}")
    if not ok:
        failures.append(name)


print("=== BMI / BSA ===")
bmi = cc.calculate_bmi(70, 175)
check("BMI 70kg/175cm", bmi, 22.857)
check_eq("BMI category 22.86", cc.bmi_category(bmi), "normal weight")
check_eq("BMI category 17", cc.bmi_category(17), "underweight")
check_eq("BMI category 32", cc.bmi_category(32), "obese (class I)")
check_eq("BMI category 37", cc.bmi_category(37), "obese (class II)")
check_eq("BMI category 42", cc.bmi_category(42), "obese (class III)")

bsa_m = cc.calculate_bsa_mosteller(70, 175)
bsa_d = cc.calculate_bsa_dubois(70, 175)
check("BSA Mosteller 70/175", bsa_m, 1.8484, tol=0.01)
check("BSA DuBois 70/175", bsa_d, 1.849, tol=0.02)
print(f"  (cross-check: Mosteller={bsa_m:.3f}, DuBois={bsa_d:.3f} -- should be close)")

print("\n=== Corrected calcium / sodium ===")
check("Corrected Ca 7.5/alb2.0", cc.corrected_calcium(7.5, 2.0), 9.1)
check("Corrected Ca normal alb (no change)", cc.corrected_calcium(9.0, 4.0), 9.0)
check("Corrected Na Katz 130/glu400", cc.corrected_sodium_katz(130, 400), 134.8)
check("Corrected Na Hillier 130/glu400", cc.corrected_sodium_hillier(130, 400), 137.2)

print("\n=== Anion gap ===")
check("AG Na140/Cl104/HCO3 24", cc.anion_gap(140, 104, 24), 12)
check("Corrected AG (alb 2.0)", cc.corrected_anion_gap(12, 2.0), 17.0)

print("\n=== Maintenance fluids (Holliday-Segar) ===")
check("Fluid rate 5kg", cc.maintenance_fluid_rate_ml_per_hr(5), 20)
check("Fluid rate 10kg (boundary)", cc.maintenance_fluid_rate_ml_per_hr(10), 40)
check("Fluid rate 15kg", cc.maintenance_fluid_rate_ml_per_hr(15), 50)
check("Fluid rate 20kg (boundary)", cc.maintenance_fluid_rate_ml_per_hr(20), 60)
check("Fluid rate 30kg", cc.maintenance_fluid_rate_ml_per_hr(30), 70)
check("Fluid rate 70kg adult", cc.maintenance_fluid_rate_ml_per_hr(70), 110)

print("\n=== QTc ===")
qtc_b_60 = cc.qtc_bazett(400, 60)
check("QTc Bazett QT400/HR60 (RR=1s, no correction)", qtc_b_60, 400)
qtc_b_100 = cc.qtc_bazett(400, 100)
check("QTc Bazett QT400/HR100", qtc_b_100, 516.4, tol=0.5)
qtc_f_100 = cc.qtc_fridericia(400, 100)
check("QTc Fridericia QT400/HR100", qtc_f_100, 474.3, tol=0.5)
print(f"  (sanity: Fridericia ({qtc_f_100:.1f}) should be < Bazett ({qtc_b_100:.1f}) at HR>60)")
check_eq("QTc interpretation 470ms male", cc.qtc_interpretation(470, "M"), "prolonged")
check_eq("QTc interpretation 450ms male", cc.qtc_interpretation(450, "M"), "prolonged")
check_eq("QTc interpretation 450ms female", cc.qtc_interpretation(450, "F"), "normal")
check_eq("QTc interpretation 430ms male", cc.qtc_interpretation(430, "M"), "normal")

print("\n=== MELD family ===")
# Classic MELD reference: bilirubin=2.0, INR=1.5, creatinine=1.2, no dialysis
meld1 = cc.calculate_meld(2.0, 1.5, 1.2, False)
check("MELD classic (bili2.0/INR1.5/cr1.2)", meld1, 15, tol=0.5)
# Lower-bound clamp check: all labs below floor should clamp to MELD 6
meld_floor = cc.calculate_meld(0.5, 0.5, 0.5, False)
check("MELD floor clamp (labs below 1.0)", meld_floor, 6, tol=0.5)
# Dialysis override: creatinine forced to 4.0 regardless of input
meld_dialysis_low = cc.calculate_meld(1.0, 1.0, 0.8, True)
meld_dialysis_manual = cc.calculate_meld(1.0, 1.0, 4.0, False)
check_eq("MELD dialysis override == manual cr=4.0", meld_dialysis_low, meld_dialysis_manual)
# Upper clamp check
meld_high = cc.calculate_meld(40, 10, 10, False)
check_eq("MELD upper clamp at 40", meld_high, 40)

# MELD-Na: should equal MELD when MELD <= 11
meld_low = cc.calculate_meld(1.0, 1.0, 1.0, False)
meld_na_low = cc.calculate_meld_na(1.0, 1.0, 1.0, 130, False)
check_eq("MELD-Na == MELD when MELD<=11", meld_na_low, meld_low)
# MELD-Na: hyponatremia should increase score above MELD when MELD>11
meld_base = cc.calculate_meld(2.0, 1.5, 1.2, False)
meld_na_hypo = cc.calculate_meld_na(2.0, 1.5, 1.2, 125, False)
print(f"  MELD-Na with hyponatremia (Na=125): base MELD={meld_base}, MELD-Na={meld_na_hypo} (should be higher)")
if meld_na_hypo <= meld_base:
    failures.append("MELD-Na should increase with hyponatremia")
    print("[FAIL] MELD-Na hyponatremia adjustment direction")
else:
    print("[PASS] MELD-Na hyponatremia adjustment direction")

# MELD 3.0: sanity checks (formula flagged lower-confidence, but internal consistency checkable)
meld3_val = cc.calculate_meld3("M", 2.0, 1.5, 1.2, 137, 3.5, False)
print(f"  MELD 3.0 (male, bili2.0/INR1.5/cr1.2/Na137/alb3.5) = {meld3_val} (no external ref verified -- flagged)")
meld3_female = cc.calculate_meld3("F", 2.0, 1.5, 1.2, 137, 3.5, False)
print(f"  MELD 3.0 female same labs = {meld3_female} (should be male+~1 due to +1.33 female term, before rounding)")
meld3_range_ok = 6 <= meld3_val <= 40 and 6 <= meld3_female <= 40
check_eq("MELD 3.0 stays within 6-40 range", meld3_range_ok, True)

print("\n=== CHA2DS2-VASc ===")
# 65F, HTN, DM -> HTN(1) + age65-74(1) + DM(1) + female(1) = 4
score1 = cc.cha2ds2_vasc_score(chf=False, hypertension=True, age=68, diabetes=True,
                                stroke_history=False, vascular_disease=False, sex_female=True)
check_eq("CHA2DS2-VASc 68F/HTN/DM", score1, 4)
# 80M, prior stroke, CHF -> age>=75(2) + stroke(2) + chf(1) = 5
score2 = cc.cha2ds2_vasc_score(chf=True, hypertension=False, age=80, diabetes=False,
                                stroke_history=True, vascular_disease=False, sex_female=False)
check_eq("CHA2DS2-VASc 80M/CHF/stroke", score2, 5)
check_eq("Risk tier 0", cc.cha2ds2_vasc_risk_tier(0), "low")
check_eq("Risk tier 1", cc.cha2ds2_vasc_risk_tier(1), "low-to-moderate")
check_eq("Risk tier 5", cc.cha2ds2_vasc_risk_tier(5), "moderate-to-high")

print("\n=== Wells' PE ===")
# Classic high-risk vignette: PE most likely dx + HR>100 + immobilization -> 3+1.5+1.5=6
wpe1 = cc.wells_pe_score(dvt_signs=False, pe_most_likely=True, hr_over_100=True,
                          immobilization_or_surgery=True, prior_dvt_pe=False, hemoptysis=False, malignancy=False)
check("Wells PE vignette (PE-likely+HR100+immob)", wpe1, 6.0)
check_eq("Wells PE interp score=6 -> moderate/likely-boundary", cc.wells_pe_interpretation(6.0),
         "PE likely (2-tier); Moderate probability (3-tier)")
check_eq("Wells PE interp score=0 -> unlikely/low", cc.wells_pe_interpretation(0.0),
         "PE unlikely (2-tier); Low probability (3-tier)")
check_eq("Wells PE interp score=7 -> likely/high", cc.wells_pe_interpretation(7.0),
         "PE likely (2-tier); High probability (3-tier)")
check_eq("Wells PE interp score=4 -> unlikely (2-tier boundary)", cc.wells_pe_interpretation(4.0).split(";")[0].strip(),
         "PE unlikely (2-tier)")

print("\n=== Wells' DVT ===")
wdvt1 = cc.wells_dvt_score(active_cancer=True, paralysis_or_immobilization=False, bedridden_or_surgery=False,
                            tenderness_deep_veins=True, entire_leg_swollen=False, calf_swelling_3cm=False,
                            pitting_edema=False, collateral_veins=False, prior_dvt=False,
                            alternative_diagnosis_as_likely=False)
check_eq("Wells DVT vignette (cancer+tenderness)", wdvt1, 2)
check_eq("Wells DVT interp score=2 -> likely/moderate", cc.wells_dvt_interpretation(2),
         "DVT likely (2-tier); Moderate probability (3-tier)")
check_eq("Wells DVT interp score=1 -> UNLIKELY (2-tier), moderate (3-tier)", cc.wells_dvt_interpretation(1),
         "DVT unlikely (2-tier); Moderate probability (3-tier)")
check_eq("Wells DVT interp score=0 -> unlikely/low", cc.wells_dvt_interpretation(0),
         "DVT unlikely (2-tier); Low probability (3-tier)")
check_eq("Wells DVT interp score=3 -> likely/high", cc.wells_dvt_interpretation(3),
         "DVT likely (2-tier); High probability (3-tier)")
# Alternative-diagnosis subtraction can go negative
wdvt2 = cc.wells_dvt_score(active_cancer=False, paralysis_or_immobilization=False, bedridden_or_surgery=False,
                            tenderness_deep_veins=False, entire_leg_swollen=False, calf_swelling_3cm=False,
                            pitting_edema=False, collateral_veins=False, prior_dvt=False,
                            alternative_diagnosis_as_likely=True)
check_eq("Wells DVT with alt-diagnosis only", wdvt2, -2)
check_eq("Wells DVT interp score=-2 -> unlikely/low", cc.wells_dvt_interpretation(-2),
         "DVT unlikely (2-tier); Low probability (3-tier)")

print("\n=== Validators (should raise CalcInputError on bad input) ===")
bad_input_tests = [
    ("weight -5", lambda: cc.validate_weight_kg(-5)),
    ("weight 'abc'", lambda: cc.validate_weight_kg("abc")),
    ("height 500", lambda: cc.validate_height_cm(500)),
    ("calcium None", lambda: cc.validate_calcium_mgdl(None)),
    ("INR 100", lambda: cc.validate_inr(100)),
]
for label, fn in bad_input_tests:
    try:
        fn()
        print(f"[FAIL] {label} did not raise")
        failures.append(f"validator {label}")
    except cc.CalcInputError:
        print(f"[PASS] {label} correctly raised CalcInputError")

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
else:
    print("ALL CHECKS PASSED")
