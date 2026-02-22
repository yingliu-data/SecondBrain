import EventKit

enum CalendarTool {
    private static let store = EKEventStore()

    static func getEvents(daysAhead: Int) async -> String {
        do {
            try await store.requestFullAccessToEvents()
        } catch {
            return "Error: Calendar access denied."
        }

        let start = Date()
        guard let end = Calendar.current.date(byAdding: .day, value: daysAhead, to: start) else {
            return "Error: Invalid date range."
        }

        let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
        let events = store.events(matching: predicate)

        if events.isEmpty { return "No upcoming events in the next \(daysAhead) days." }

        let fmt = DateFormatter()
        fmt.dateFormat = "E MMM d, h:mm a"

        return events.map { event in
            let duration = Int(event.endDate.timeIntervalSince(event.startDate) / 60)
            return "• \(fmt.string(from: event.startDate)) — \(event.title ?? "Untitled") (\(duration) min)"
        }.joined(separator: "\n")
    }

    static func createEvent(title: String, startDate: String, duration: Int) async -> String {
        do {
            try await store.requestFullAccessToEvents()
        } catch {
            return "Error: Calendar access denied."
        }

        guard let start = ISO8601DateFormatter().date(from: startDate) else {
            return "Error: Invalid date format. Use ISO 8601 (e.g. 2026-02-22T15:00:00)."
        }

        let event = EKEvent(eventStore: store)
        event.title = title
        event.startDate = start
        event.endDate = start.addingTimeInterval(TimeInterval(duration * 60))
        event.calendar = store.defaultCalendarForNewEvents

        do {
            try store.save(event, span: .thisEvent)
            return "Created '\(title)' on \(startDate)."
        } catch {
            return "Error creating event: \(error.localizedDescription)"
        }
    }
}
