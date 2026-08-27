"""
A curated list of common generic + brand drug names used for inline-query
autocomplete suggestions.

Why local instead of live openFDA wildcard search: testing showed openFDA's
harmonized `openfda.*` fields don't reliably support prefix wildcards (a
query like `openfda.generic_name:ser*` returned the ENTIRE unfiltered
database rather than matches starting with "ser"). A local list is instant,
has zero network latency per keystroke, and avoids hitting openFDA's rate
limit on every character typed.

This list only powers the autocomplete dropdown. The actual /dose lookup
(drug_lookup.lookup_drug) always queries openFDA live with an exact quoted
match, so a drug NOT in this list can still be looked up directly by typing
"/dose <name>" -- this list just won't suggest it as you type.

Extend this list freely; it's plain Python, no build step needed.
"""

COMMON_DRUGS = [
    # --- Antibiotics / antimicrobials ---
    "Amoxicillin", "Amoxicillin Clavulanate", "Ampicillin", "Cephalexin",
    "Cefazolin", "Ceftriaxone", "Cefdinir", "Cefepime", "Azithromycin",
    "Clarithromycin", "Erythromycin", "Ciprofloxacin", "Levofloxacin",
    "Moxifloxacin", "Doxycycline", "Minocycline", "Tetracycline",
    "Metronidazole", "Clindamycin", "Vancomycin", "Gentamicin", "Tobramycin",
    "Amikacin", "Piperacillin Tazobactam", "Meropenem", "Imipenem",
    "Trimethoprim Sulfamethoxazole", "Nitrofurantoin", "Linezolid",
    "Rifampin", "Isoniazid", "Ethambutol", "Pyrazinamide", "Fluconazole",
    "Itraconazole", "Voriconazole", "Nystatin", "Acyclovir", "Valacyclovir",
    "Oseltamivir",
    # --- Cardiology ---
    "Lisinopril", "Enalapril", "Losartan", "Valsartan", "Amlodipine",
    "Nifedipine", "Metoprolol", "Atenolol", "Carvedilol", "Propranolol",
    "Atorvastatin", "Rosuvastatin", "Simvastatin", "Furosemide",
    "Hydrochlorothiazide", "Spironolactone", "Digoxin", "Amiodarone",
    "Warfarin", "Clopidogrel", "Apixaban", "Rivaroxaban", "Dabigatran",
    "Aspirin", "Nitroglycerin", "Diltiazem", "Verapamil", "Sacubitril Valsartan",
    # --- Endocrinology / diabetes ---
    "Metformin", "Insulin", "Glipizide", "Glyburide", "Sitagliptin",
    "Empagliflozin", "Semaglutide", "Liraglutide", "Levothyroxine",
    "Methimazole", "Propylthiouracil", "Prednisone", "Hydrocortisone",
    "Dexamethasone",
    # --- Gastroenterology ---
    "Omeprazole", "Pantoprazole", "Esomeprazole", "Ranitidine", "Famotidine",
    "Ondansetron", "Metoclopramide", "Loperamide", "Docusate", "Polyethylene Glycol",
    "Lactulose", "Mesalamine", "Sulfasalazine",
    # --- Pulmonology / allergy ---
    "Albuterol", "Ipratropium", "Fluticasone", "Budesonide", "Montelukast",
    "Theophylline", "Diphenhydramine", "Loratadine", "Cetirizine", "Fexofenadine",
    "Prednisolone",
    # --- Neurology ---
    "Levetiracetam", "Phenytoin", "Valproate", "Carbamazepine", "Lamotrigine",
    "Gabapentin", "Pregabalin", "Topiramate", "Sumatriptan", "Donepezil",
    "Memantine", "Levodopa Carbidopa", "Baclofen",
    # --- Psychiatry ---
    "Sertraline", "Fluoxetine", "Escitalopram", "Citalopram", "Paroxetine",
    "Venlafaxine", "Duloxetine", "Bupropion", "Mirtazapine", "Trazodone",
    "Vortioxetine", "Vilazodone", "Amitriptyline", "Nortriptyline",
    "Risperidone", "Olanzapine", "Quetiapine", "Aripiprazole", "Ziprasidone",
    "Clozapine", "Haloperidol", "Lithium", "Alprazolam", "Clonazepam",
    "Diazepam", "Lorazepam", "Buspirone", "Zolpidem", "Methylphenidate",
    "Amphetamine", "Atomoxetine", "Guanfacine", "Clonidine",
    # --- Pain / anesthesia ---
    "Ibuprofen", "Acetaminophen", "Naproxen", "Celecoxib", "Tramadol",
    "Morphine", "Oxycodone", "Hydrocodone", "Fentanyl", "Methadone",
    "Buprenorphine", "Naloxone", "Naltrexone", "Ketorolac", "Lidocaine",
    # --- Urology / renal ---
    "Tamsulosin", "Finasteride", "Sildenafil", "Tadalafil", "Oxybutynin",
    # --- OB/GYN ---
    "Ethinyl Estradiol Norethindrone", "Medroxyprogesterone", "Misoprostol",
    "Oxytocin", "Magnesium Sulfate",
    # --- Dermatology ---
    "Hydrocortisone Topical", "Triamcinolone", "Clindamycin Topical",
    "Tretinoin", "Isotretinoin", "Doxycycline",
    # --- Oncology (common supportive/chemo) ---
    "Tamoxifen", "Methotrexate", "Cyclophosphamide", "Ondansetron",
    "Filgrastim",
    # --- Vaccines / immunizations (common lookups) ---
    "Influenza Vaccine", "Tetanus Toxoid",
]

# Dedupe while preserving order, in case a drug is relevant to multiple
# specialties above (e.g. Doxycycline appears in both antibiotics and derm).
_seen = set()
COMMON_DRUGS = [d for d in COMMON_DRUGS if not (d.lower() in _seen or _seen.add(d.lower()))]


def search_common_drugs(prefix: str, limit: int = 8) -> list[str]:
    """Case-insensitive prefix match against the local drug list."""
    prefix = prefix.strip().lower()
    if len(prefix) < 2:
        return []
    matches = [d for d in COMMON_DRUGS if d.lower().startswith(prefix)]
    return matches[:limit]
