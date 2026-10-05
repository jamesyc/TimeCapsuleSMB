import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

final class OutputLineParserTests: XCTestCase {
    func testParserHandlesSplitMultipleAndUnterminatedLines() {
        var parser = OutputLineParser()

        var events: [BackendEvent] = []
        events.append(contentsOf: parser.append(Data(#"{"type":"stage","operation":"capabilities","stage":"resolve"#.utf8)))
        events.append(contentsOf: parser.append(Data(#"_paths"}"#.utf8)))
        events.append(contentsOf: parser.append(Data("\nnot-json\n".utf8)))
        events.append(contentsOf: parser.append(Data(#"{"type":"result","operation":"capabilities","ok":true,"payload":{}}"#.utf8)))
        events.append(contentsOf: parser.finish())

        XCTAssertEqual(events.map(\.type), ["stage", "result"])
        XCTAssertEqual(events.first?.stage, "resolve_paths")
        XCTAssertEqual(events.last?.ok, true)
    }

    func testParserDecodesMigrationProgress() {
        var parser = OutputLineParser()
        let events = parser.append(Data((#"{"schema_version":1,"type":"progress","operation":"deploy","request_id":"r","#
            + #""stage":"migrate_xattrs_copy","entries":120000}"# + "\n").utf8))

        XCTAssertEqual(events.count, 1)
        XCTAssertEqual(events[0].type, "progress")
        XCTAssertEqual(events[0].stage, "migrate_xattrs_copy")
        XCTAssertEqual(events[0].entries, 120000)
    }
}
