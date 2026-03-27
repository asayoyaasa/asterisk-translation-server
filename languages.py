"""
Language detection from E.164 phone number prefix.
Maps country calling codes to language names for OpenAI translation sessions.
Tries 3-digit prefix first, then 2-digit, then 1-digit. Defaults to English.
"""

COUNTRY_LANGUAGES = {
    # 3-digit prefixes (must come first)
    "355": "Albanian",
    "213": "Arabic",
    "376": "Catalan",
    "501": "English",  # Belize
    "229": "French",   # Benin
    "267": "English",  # Botswana
    "975": "Dzongkha",
    "591": "Spanish",  # Bolivia
    "387": "Bosnian",
    "267": "English",  # Botswana
    "55":  "Portuguese",
    "673": "Malay",
    "359": "Bulgarian",
    "226": "French",   # Burkina Faso
    "257": "French",   # Burundi
    "855": "Khmer",
    "237": "French",   # Cameroon
    "238": "Portuguese",  # Cape Verde
    "236": "French",   # Central African Republic
    "235": "French",   # Chad
    "56":  "Spanish",  # Chile
    "86":  "Chinese",
    "57":  "Spanish",  # Colombia
    "269": "French",   # Comoros
    "242": "French",   # Congo
    "243": "French",   # DR Congo
    "506": "Spanish",  # Costa Rica
    "385": "Croatian",
    "53":  "Spanish",  # Cuba
    "357": "Greek",    # Cyprus
    "420": "Czech",
    "45":  "Danish",
    "253": "French",   # Djibouti
    "593": "Spanish",  # Ecuador
    "20":  "Arabic",   # Egypt
    "503": "Spanish",  # El Salvador
    "240": "Spanish",  # Equatorial Guinea
    "291": "Tigrinya",
    "372": "Estonian",
    "251": "Amharic",
    "679": "Fijian",
    "358": "Finnish",
    "33":  "French",
    "241": "French",   # Gabon
    "220": "English",  # Gambia
    "995": "Georgian",
    "49":  "German",
    "233": "English",  # Ghana
    "30":  "Greek",
    "502": "Spanish",  # Guatemala
    "224": "French",   # Guinea
    "245": "Portuguese",  # Guinea-Bissau
    "592": "English",  # Guyana
    "509": "French",   # Haiti
    "504": "Spanish",  # Honduras
    "36":  "Hungarian",
    "354": "Icelandic",
    "91":  "Hindi",
    "62":  "Indonesian",
    "98":  "Persian",
    "964": "Arabic",   # Iraq
    "353": "English",  # Ireland
    "972": "Hebrew",
    "39":  "Italian",
    "225": "French",   # Ivory Coast
    "81":  "Japanese",
    "962": "Arabic",   # Jordan
    "254": "Swahili",  # Kenya
    "965": "Arabic",   # Kuwait
    "996": "Kyrgyz",
    "856": "Lao",
    "371": "Latvian",
    "961": "Arabic",   # Lebanon
    "266": "Sesotho",
    "231": "English",  # Liberia
    "218": "Arabic",   # Libya
    "423": "German",   # Liechtenstein
    "370": "Lithuanian",
    "352": "Luxembourgish",
    "261": "Malagasy",
    "265": "English",  # Malawi
    "60":  "Malay",
    "960": "Dhivehi",
    "223": "French",   # Mali
    "356": "Maltese",
    "222": "Arabic",   # Mauritania
    "230": "English",  # Mauritius
    "52":  "Spanish",  # Mexico
    "373": "Romanian", # Moldova
    "976": "Mongolian",
    "382": "Montenegrin",
    "212": "Arabic",   # Morocco
    "258": "Portuguese",  # Mozambique
    "264": "English",  # Namibia
    "977": "Nepali",
    "31":  "Dutch",
    "64":  "English",  # New Zealand
    "505": "Spanish",  # Nicaragua
    "227": "French",   # Niger
    "234": "English",  # Nigeria
    "47":  "Norwegian",
    "968": "Arabic",   # Oman
    "92":  "Urdu",
    "507": "Spanish",  # Panama
    "675": "English",  # Papua New Guinea
    "595": "Spanish",  # Paraguay
    "51":  "Spanish",  # Peru
    "63":  "Filipino",
    "48":  "Polish",
    "351": "Portuguese",
    "974": "Arabic",   # Qatar
    "40":  "Romanian",
    "7":   "Russian",
    "250": "Kinyarwanda",
    "966": "Arabic",   # Saudi Arabia
    "221": "French",   # Senegal
    "381": "Serbian",
    "232": "English",  # Sierra Leone
    "65":  "English",  # Singapore
    "421": "Slovak",
    "386": "Slovenian",
    "252": "Somali",
    "27":  "Afrikaans",
    "82":  "Korean",
    "34":  "Spanish",
    "94":  "Sinhala",
    "249": "Arabic",   # Sudan
    "597": "Dutch",    # Suriname
    "268": "Swati",
    "46":  "Swedish",
    "41":  "German",   # Switzerland
    "963": "Arabic",   # Syria
    "886": "Chinese",  # Taiwan
    "992": "Tajik",
    "255": "Swahili",  # Tanzania
    "66":  "Thai",
    "228": "French",   # Togo
    "216": "Arabic",   # Tunisia
    "90":  "Turkish",
    "993": "Turkmen",
    "256": "English",  # Uganda
    "380": "Ukrainian",
    "971": "Arabic",   # UAE
    "44":  "English",  # UK
    "1":   "English",  # USA/Canada
    "598": "Spanish",  # Uruguay
    "998": "Uzbek",
    "58":  "Spanish",  # Venezuela
    "84":  "Vietnamese",
    "967": "Arabic",   # Yemen
    "260": "English",  # Zambia
    "263": "English",  # Zimbabwe
}


def get_language(phone_number: str) -> str:
    """
    Detect language from E.164 phone number.
    Strips leading + if present.
    Tries 3-digit prefix, then 2-digit, then 1-digit.
    Returns 'English' if not found.
    """
    if not phone_number:
        return "English"

    number = phone_number.lstrip("+").strip()

    # Try longest prefix first
    for length in (3, 2, 1):
        prefix = number[:length]
        if prefix in COUNTRY_LANGUAGES:
            return COUNTRY_LANGUAGES[prefix]

    return "English"


if __name__ == "__main__":
    # Quick test
    tests = [
        ("6281291960446", "Indonesian"),
        ("15142046067",   "English"),
        ("521234567890",  "Spanish"),
        ("4412345678",    "English"),
        ("8613912345678", "Chinese"),
        ("917012345678",  "Hindi"),
        ("841234567890",  "Vietnamese"),
    ]
    for num, expected in tests:
        result = get_language(num)
        status = "✓" if result == expected else "✗"
        print(f"{status} {num[:4]}... → {result} (expected {expected})")
