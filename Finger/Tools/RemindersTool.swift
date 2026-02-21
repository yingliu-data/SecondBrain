import EventKit

enum RemindersTool {
    private static let store = EKEventStore()

    static func getPending() async -> String {
        do {
            try await store.requestFullAccessToReminders()
        } catch {
            return "Error: Reminders access denied."
        }

        return await withCheckedContinuation { cont in
            let predicate = store.predicateForIncompleteReminders(
                withDueDateStarting: nil, ending: nil, calendars: nil
            )
            store.fetchReminders(matching: predicate) { reminders in
                guard let reminders, !reminders.isEmpty else {
                    cont.resume(returning: "No pending reminders.")
                    return
                }
                let text = reminders.prefix(20).map { "• \($0.title ?? "Untitled")" }
                    .joined(separator: "\n")
                cont.resume(returning: text)
            }
        }
    }

    static func create(title: String, dueDate: String?) async -> String {
        do {
            try await store.requestFullAccessToReminders()
        } catch {
            return "Error: Reminders access denied."
        }

        let reminder = EKReminder(eventStore: store)
        reminder.title = title
        reminder.calendar = store.defaultCalendarForNewReminders()

        if let dueDate, let date = ISO8601DateFormatter().date(from: dueDate) {
            reminder.dueDateComponents = Calendar.current.dateComponents(
                [.year, .month, .day, .hour, .minute], from: date
            )
        }

        do {
            try store.save(reminder, commit: true)
            return "Created reminder '\(title)'."
        } catch {
            return "Error creating reminder: \(error.localizedDescription)"
        }
    }
}
