@isTest
private class EventRegistrationControllerTest {

    @isTest
    static void testSuccessfulRegistration() {
        Test.startTest();
        Id newId = EventRegistrationController.submitRegistration(
            'Jane Doe', 'jane.doe@example.com', '555-123-4567', 4, Date.today().addDays(10)
        );
        Test.stopTest();

        System.assertNotEquals(null, newId, 'A new registration record should have been created.');
        Event_Registration__c reg = [SELECT Attendee_Name__c, Email__c, Status__c, Number_of_Guests__c
                                      FROM Event_Registration__c WHERE Id = :newId];
        System.assertEquals('Jane Doe', reg.Attendee_Name__c);
        System.assertEquals('jane.doe@example.com', reg.Email__c);
        System.assertEquals('Pending', reg.Status__c);
        System.assertEquals(4, reg.Number_of_Guests__c);
    }

    @isTest
    static void testBlankNameIsRejected() {
        Boolean threw = false;
        try {
            EventRegistrationController.submitRegistration(
                '', 'jane.doe@example.com', null, 2, Date.today().addDays(5)
            );
        } catch (AuraHandledException e) {
            threw = true;
        }
        System.assert(threw, 'Blank attendee name should be rejected.');
    }

    @isTest
    static void testInvalidEmailIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'notanemail', null, 2, Date.today().addDays(5)
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('A valid email address is required.', errorMessage,
            'CLAUDE-15: Invalid email format should be rejected with a clear message.');
    }

    @isTest
    static void testZeroGuestsIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'john@example.com', null, 0, Date.today().addDays(5)
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('Number of guests must be between 1 and 10.', errorMessage,
            'CLAUDE-16: Zero guests should be rejected with the guest-count validation message.');
    }

    @isTest
    static void testNegativeGuestsIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'john@example.com', null, -3, Date.today().addDays(5)
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('Number of guests must be between 1 and 10.', errorMessage,
            'CLAUDE-16: Negative guests should be rejected with the guest-count validation message.');
    }

    @isTest
    static void testMinimumBoundaryGuestsIsAccepted() {
        Test.startTest();
        Id newId = EventRegistrationController.submitRegistration(
            'Boundary Tester', 'minimum@example.com', null, 1, Date.today().addDays(5)
        );
        Test.stopTest();

        System.assertNotEquals(null, newId, 'CLAUDE-16: Exactly 1 guest (the minimum boundary) should be accepted.');
    }

    @isTest
    static void testTooManyGuestsIsRejected() {
        Boolean threw = false;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'john@example.com', null, 500, Date.today().addDays(5)
            );
        } catch (AuraHandledException e) {
            threw = true;
        }
        System.assert(threw, 'More than 10 guests should be rejected.');
    }

    @isTest
    static void testMaxBoundaryGuestsIsAccepted() {
        Test.startTest();
        Id newId = EventRegistrationController.submitRegistration(
            'Boundary Tester', 'boundary@example.com', null, 10, Date.today().addDays(5)
        );
        Test.stopTest();
        System.assertNotEquals(null, newId, 'Exactly 10 guests (the max boundary) should be accepted.');
    }

    @isTest
    static void testPastEventDateIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'john@example.com', null, 2, Date.today().addDays(-1)
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('Event date must be in the future.', errorMessage,
            'CLAUDE-17: A past event date should be rejected with the expected message.');
    }

    @isTest
    static void testTodayEventDateIsRejected() {
        String errorMessage;
        try {
            EventRegistrationController.submitRegistration(
                'John Smith', 'john@example.com', null, 2, Date.today()
            );
        } catch (AuraHandledException e) {
            errorMessage = e.getMessage();
        }
        System.assertEquals('Event date must be in the future.', errorMessage,
            'CLAUDE-17: A same-day event date should be rejected with the expected message.');
    }

    @isTest
    static void testGetRecentRegistrationsReturnsNewestFirst() {
        EventRegistrationController.submitRegistration(
            'First Person', 'first@example.com', null, 2, Date.today().addDays(5)
        );
        EventRegistrationController.submitRegistration(
            'Second Person', 'second@example.com', null, 3, Date.today().addDays(6)
        );

        Test.startTest();
        List<Event_Registration__c> results = EventRegistrationController.getRecentRegistrations();
        Test.stopTest();

        System.assertEquals(2, results.size(), 'Both registrations should be returned.');
        System.assertEquals('Second Person', results[0].Attendee_Name__c,
            'Most recently created registration should be first.');
    }
}
