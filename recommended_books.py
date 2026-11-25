"""
Standard reference books per specialty, offered to the admin in the mini app
(Ask your books > Manage specialty libraries > Recommended books). Picking
one lets the admin upload their own PDF of it straight into that specialty,
under its proper title. Nothing is downloaded: the list is only titles.

"match" holds the words that identify the book in an existing title, so a
book already in the library shows as added (all words must appear).
"""

RECOMMENDED: dict[str, list[dict]] = {
    "Internal Medicine": [
        {"title": "Harrison's Principles of Internal Medicine", "by": "Loscalzo, Fauci, Kasper et al. (McGraw Hill)", "match": ["harrison"]},
        {"title": "Goldman-Cecil Medicine", "by": "Goldman & Cooney (Elsevier)", "match": ["cecil"]},
        {"title": "Davidson's Principles and Practice of Medicine", "by": "Penman, Ralston, Strachan, Hobson (Elsevier)", "match": ["davidson"]},
        {"title": "Kumar & Clark's Clinical Medicine", "by": "Feather, Randall, Waterhouse (Elsevier)", "match": ["kumar", "clark"]},
        {"title": "CURRENT Medical Diagnosis & Treatment", "by": "Papadakis, McPhee, Rabow (McGraw Hill)", "match": ["current medical diagnosis"]},
        {"title": "The Washington Manual of Medical Therapeutics", "by": "Washington University (Wolters Kluwer)", "match": ["washington", "manual"]},
        {"title": "Oxford Handbook of Clinical Medicine", "by": "Wilkinson et al. (Oxford University Press)", "match": ["oxford", "handbook", "clinical", "medicine"]},
        {"title": "Oxford Textbook of Medicine", "by": "Firth, Conlon, Cox (Oxford University Press)", "match": ["oxford", "textbook", "medicine"]},
        {"title": "Macleod's Clinical Examination", "by": "Innes, Dover, Fairhurst (Elsevier)", "match": ["macleod"]},
        {"title": "MKSAP: Medical Knowledge Self-Assessment Program", "by": "American College of Physicians", "match": ["mksap"]},
    ],
    "Cardiology": [
        {"title": "Braunwald's Heart Disease: A Textbook of Cardiovascular Medicine", "by": "Libby, Bonow, Mann, Tomaselli et al. (Elsevier)", "match": ["braunwald"]},
        {"title": "Hurst's The Heart", "by": "Fuster, Harrington, Narula, Eapen (McGraw Hill)", "match": ["hurst"]},
        {"title": "Topol's Textbook of Cardiovascular Medicine", "by": "Topol (Wolters Kluwer)", "match": ["topol"]},
        {"title": "The ESC Textbook of Cardiovascular Medicine", "by": "European Society of Cardiology (Oxford University Press)", "match": ["esc textbook"]},
        {"title": "Manual of Cardiovascular Medicine", "by": "Griffin, Topol et al. (Wolters Kluwer)", "match": ["manual", "cardiovascular", "medicine"]},
        {"title": "Feigenbaum's Echocardiography", "by": "Armstrong & Ryan (Wolters Kluwer)", "match": ["feigenbaum"]},
        {"title": "Marriott's Practical Electrocardiography", "by": "Wagner & Strauss (Wolters Kluwer)", "match": ["marriott"]},
        {"title": "Goldberger's Clinical Electrocardiography: A Simplified Approach", "by": "Goldberger, Shvilkin, Goldberger (Elsevier)", "match": ["goldberger"]},
        {"title": "Grossman & Baim's Cardiac Catheterization, Angiography, and Intervention", "by": "Kern et al. (Wolters Kluwer)", "match": ["grossman", "baim"]},
        {"title": "Oxford Handbook of Cardiology", "by": "Ramrakha & Hill (Oxford University Press)", "match": ["oxford", "handbook", "cardiology"]},
    ],
    "Pediatrics": [
        {"title": "Nelson Textbook of Pediatrics", "by": "Kliegman, St. Geme et al. (Elsevier)", "match": ["nelson", "textbook"]},
        {"title": "Nelson Essentials of Pediatrics", "by": "Marcdante & Kliegman (Elsevier)", "match": ["nelson", "essentials"]},
        {"title": "Rudolph's Pediatrics", "by": "Kline et al. (McGraw Hill)", "match": ["rudolph"]},
        {"title": "The Harriet Lane Handbook", "by": "Johns Hopkins Hospital (Elsevier)", "match": ["harriet", "lane"]},
        {"title": "Red Book: Report of the Committee on Infectious Diseases", "by": "American Academy of Pediatrics", "match": ["red book"]},
        {"title": "Avery's Diseases of the Newborn", "by": "Gleason & Juul (Elsevier)", "match": ["avery"]},
        {"title": "Fanaroff and Martin's Neonatal-Perinatal Medicine", "by": "Martin, Fanaroff, Walsh (Elsevier)", "match": ["fanaroff"]},
        {"title": "Fleisher & Ludwig's Textbook of Pediatric Emergency Medicine", "by": "Shaw & Bachur (Wolters Kluwer)", "match": ["fleisher", "ludwig"]},
        {"title": "Smith's Recognizable Patterns of Human Malformation", "by": "Jones, Jones, del Campo (Elsevier)", "match": ["recognizable", "patterns"]},
        {"title": "Illustrated Textbook of Paediatrics", "by": "Lissauer & Carroll (Elsevier)", "match": ["illustrated", "paediatrics"]},
    ],
    "Surgery": [
        {"title": "Sabiston Textbook of Surgery", "by": "Townsend, Beauchamp, Evers, Mattox (Elsevier)", "match": ["sabiston"]},
        {"title": "Schwartz's Principles of Surgery", "by": "Brunicardi et al. (McGraw Hill)", "match": ["schwartz"]},
        {"title": "Bailey & Love's Short Practice of Surgery", "by": "Williams, O'Connell, McCaskie (CRC Press)", "match": ["bailey", "love"]},
        {"title": "Greenfield's Surgery: Scientific Principles and Practice", "by": "Mulholland et al. (Wolters Kluwer)", "match": ["greenfield"]},
        {"title": "CURRENT Diagnosis & Treatment: Surgery", "by": "Doherty (McGraw Hill)", "match": ["current", "treatment", "surgery"]},
        {"title": "Maingot's Abdominal Operations", "by": "Zinner & Ashley (McGraw Hill)", "match": ["maingot"]},
        {"title": "Fischer's Mastery of Surgery", "by": "Fischer et al. (Wolters Kluwer)", "match": ["mastery", "surgery"]},
        {"title": "Zollinger's Atlas of Surgical Operations", "by": "Ellison & Zollinger (McGraw Hill)", "match": ["zollinger"]},
        {"title": "ATLS: Advanced Trauma Life Support Student Course Manual", "by": "American College of Surgeons", "match": ["atls"]},
        {"title": "Oxford Handbook of Clinical Surgery", "by": "McLatchie, Borley, Chikwe (Oxford University Press)", "match": ["oxford", "handbook", "surgery"]},
    ],
}
