"""agent/test_phase1_lang.py - tests for plain-language matching (v2),
layered on top of the existing Phase 1 fixture pattern: builds its own
disposable repo, never touches a real one.
Run: python test_phase1_lang.py
"""
import os, sys, shutil, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_index, localizer  # noqa

FIXTURE = {
    "force-app/main/default/classes/EventRegistrationController.cls": """
public with sharing class EventRegistrationController {
    public static void submitRegistration(Event_Registration__c r) {
        if (!isValidEmail(r.Email__c)) { throw new RegistrationException('Invalid email'); }
        if (r.Number_of_Guests__c < 0) { throw new RegistrationException('Bad guests'); }
    }
}
""",
    "force-app/main/default/classes/EventRegistrationControllerTest.cls": """
@IsTest
private class EventRegistrationControllerTest {
    @IsTest static void testSubmit() { System.assert(true); }
}
""",
    "force-app/main/default/objects/Event_Registration__c/Event_Registration__c.object-meta.xml":
        '<?xml version="1.0"?><CustomObject><fullName>Event_Registration__c</fullName><label>Event Registration</label></CustomObject>',
    "force-app/main/default/objects/Event_Registration__c/fields/Email__c.field-meta.xml":
        '<?xml version="1.0"?><CustomField><fullName>Email__c</fullName><label>Email Address</label></CustomField>',
    "force-app/main/default/objects/Event_Registration__c/fields/Number_of_Guests__c.field-meta.xml":
        '<?xml version="1.0"?><CustomField><fullName>Number_of_Guests__c</fullName><label>Number of Guests</label></CustomField>',
}


def make_repo():
    root = tempfile.mkdtemp(prefix="phase1-lang-")
    for rel, content in FIXTURE.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    return root


class WordSplitting(unittest.TestCase):
    def test_camel_case_split(self):
        self.assertEqual(code_index.split_words("EventRegistrationController"),
                          ["event", "registration", "controller"])

    def test_field_api_name_split(self):
        self.assertEqual(code_index.split_words("Number_of_Guests__c"),
                          ["number", "of", "guests"])

    def test_lwc_camel_split(self):
        self.assertEqual(code_index.split_words("eventRegistrationForm"),
                          ["event", "registration", "form"])


class PlainLanguageMatching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = make_repo()
        cls.idx = code_index.build_or_update(cls.repo, force=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.repo, ignore_errors=True)

    def test_field_inherits_parent_object_words(self):
        key = [k for k in self.idx["files"]
               if k.replace("\\", "/").endswith("Event_Registration__c/fields/Email__c.field-meta.xml")][0]
        e = self.idx["files"][key]
        self.assertIn("email", e["words"])
        self.assertIn("registration", e["words"])  # inherited from parent object

    def test_plain_ticket_no_jargon_proceeds(self):
        r = localizer.localize(self.idx, "Email format validation is not enforced on the registration form")
        self.assertGreaterEqual(r["confidence"], 0.35)
        self.assertTrue(any("Email__c" in c["path"] for c in r["candidates"][:1]))

    def test_casual_phrasing_proceeds(self):
        r = localizer.localize(self.idx, "Users can submit event registrations with an invalid email address")
        self.assertGreaterEqual(r["confidence"], 0.35)
        self.assertIn("Email__c", r["candidates"][0]["path"])

    def test_guest_count_plain_english_proceeds(self):
        r = localizer.localize(self.idx, "When I register for an event and put in a negative number of guests, "
                                          "the system still lets me submit it")
        self.assertGreaterEqual(r["confidence"], 0.35)
        self.assertIn("Number_of_Guests__c", r["candidates"][0]["path"])

    def test_plural_variant_matches(self):
        hits = code_index.find_by_word(self.idx, "registrations")  # plural, index has "registration"
        self.assertTrue(hits)

    def test_truly_vague_still_refused(self):
        r = localizer.localize(self.idx, "The app is broken, please fix")
        self.assertLess(r["confidence"], 0.35)

    def test_only_generic_words_still_refused(self):
        r = localizer.localize(self.idx, "The controller and the form and the record are not working")
        self.assertLess(r["confidence"], 0.35)

    def test_generic_word_alone_does_not_dominate(self):
        # "controller" matches 2 files but is generic - must not outscore a
        # ticket with zero real signal into proceeding territory
        r = localizer.localize(self.idx, "the controller is broken")
        self.assertLess(r["confidence"], 0.35)


if __name__ == "__main__":
    unittest.main(verbosity=2)
