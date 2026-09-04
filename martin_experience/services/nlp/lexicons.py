"""
Taxonomy-grounded lexicons for the baseline NLP (spec §11.2). These bootstrap topic + aspect
sentiment deterministically until trained compact models replace them (spec §11.2, §13.C).
Keys are taxonomy topic_ids from taxonomy_v1.yaml.
"""

# topic_id -> trigger phrases (lowercased). Order-independent substring match.
TOPIC_LEXICON: dict[str, list[str]] = {
    "WAIT.TOTAL_WAIT": ["waited", "waiting", "long wait", "minutes past", "hour wait", "sat for"],
    "WAIT.DELAY_COMMUNICATION": ["told me why", "nobody told", "no one told", "no update",
                                 "kept informed", "why i waited", "communication about the delay"],
    "WAIT.PERCEIVED_WAIT": ["felt like forever", "seemed long"],
    "ACCESS.SCHEDULING": ["scheduling", "schedule", "reschedule", "book an appointment",
                          "get an appointment", "booking"],
    "ACCESS.APPOINTMENT_AVAILABILITY": ["availability", "next available", "no openings"],
    "ACCESS.PHONE_ACCESS": ["phone", "called", "call ", "get through", "on hold", "the line"],
    "ACCESS.REFERRAL": ["referral", "referred"],
    "ARRIVAL.FRONT_DESK": ["front desk", "reception", "receptionist"],
    "ARRIVAL.CHECK_IN": ["check-in", "check in", "checked in", "checkin"],
    "ARRIVAL.PARKING": ["parking", "park", "garage", "valet"],
    "ARRIVAL.WAYFINDING": ["signage", "directions", "got lost", "hard to find"],
    "CLIN.PHYSICIAN_COMMUNICATION": ["doctor", "physician", "surgeon", "the dr"],
    "CLIN.NURSING_COMMUNICATION": ["nurse", "nursing"],
    "CLIN.EXPLANATION_OF_CARE": ["explained", "explain", "explanation", "clearly", "made it clear"],
    "CLIN.LISTENING": ["listened", "heard me", "did not listen"],
    "CLIN.EMPATHY": ["caring", "compassion", "empathy", "cared"],
    "CLIN.RESPECT": ["respectful", "disrespect", "rude to me"],
    "RESP.RESPONSE_TIME": ["response time", "responsive", "call light", "took forever to respond"],
    "COORD.DISCHARGE": ["discharge", "discharged", "sent home"],
    "COORD.MEDICATION_EXPLANATION": ["medication", "medicine", "prescription"],
    "COORD.FOLLOW_UP_INSTRUCTIONS": ["follow-up instructions", "aftercare", "what to do next"],
    "ENV.CLEANLINESS": ["clean", "dirty", "filthy", "spotless", "sanitary"],
    "ENV.NOISE": ["noisy", "loud", "quiet", "noise"],
    "ENV.FOOD": ["food", "meal", "cafeteria"],
    "ENV.COMFORT": ["comfortable", "uncomfortable", "cold room"],
    "FIN.BILLING": ["bill", "billing", "invoice", "charge", "statement"],
    "FIN.INSURANCE": ["insurance", "coverage", "copay", "co-pay"],
    "FIN.PRICE_TRANSPARENCY": ["estimate", "price upfront", "cost upfront", "how much it would cost"],
    "DIG.PATIENT_PORTAL": ["portal", "mychart", "logged out", "logging me out", "log in", "login"],
    "DIG.MESSAGING": ["message my doctor", "messaging", "send a message", "contact my doctor"],
    "DIG.ONLINE_SCHEDULING": ["schedule online", "book online", "online scheduling"],
    "DIG.WEBSITE": ["website", "web site"],
    "DIG.MOBILE_APP": ["the app", "mobile app"],
    "STAFF.COURTESY": ["courteous", "courtesy", "polite", "rude", "friendly", "nasty"],
    "STAFF.PROFESSIONALISM": ["professional", "unprofessional"],
    "STAFF.TEAMWORK": ["teamwork", "worked together", "no one knew"],
    "REP.LIKELIHOOD_TO_RECOMMEND": ["recommend", "tell my friends", "would come back"],
}

POSITIVE = {
    "wonderful", "great", "excellent", "amazing", "clean", "spotless", "courteous", "polite",
    "friendly", "fast", "quick", "easy", "clearly", "clear", "good", "helpful", "professional",
    "caring", "kind", "smooth", "explained", "respectful", "comfortable", "recommend", "pleasant",
}
NEGATIVE = {
    "nightmare", "confusing", "confused", "dirty", "filthy", "rude", "slow", "long", "waited",
    "wait", "never", "failed", "unprofessional", "ignored", "forever", "terrible", "awful", "bad",
    "frustrating", "frustrated", "difficult", "hard", "cold", "noisy", "loud", "disrespect",
    "problem", "issue", "lost", "worst", "unacceptable",
}
NEGATORS = {"not", "no", "never", "nobody", "couldn't", "cannot", "can't", "didn't", "wasn't",
            "without", "none", "no one"}

# multi-word negative cues that count as one negative signal
NEG_PHRASES = ["could not", "kept logging", "logging me out", "logged out", "get through",
               "no one told", "nobody told", "on hold", "minutes past", "never get"]
