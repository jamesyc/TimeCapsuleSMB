import Foundation
import XCTest
@testable import TimeCapsuleSMBApp

final class HelperPipeReaderTests: XCTestCase {
    func testReadabilityPipeReaderStreamsChunksUntilWriterCloses() async throws {
        let pipe = Pipe()
        let reader = ReadabilityPipeReader()
        let task = Task {
            var chunks: [Data] = []
            for try await data in reader.chunks(from: pipe.fileHandleForReading) {
                chunks.append(data)
            }
            return chunks
        }

        try pipe.fileHandleForWriting.write(contentsOf: Data("first\n".utf8))
        try pipe.fileHandleForWriting.write(contentsOf: Data("second\n".utf8))
        try pipe.fileHandleForWriting.close()

        let chunks = try await task.value
        let combined = chunks.reduce(into: Data()) { partial, chunk in
            partial.append(chunk)
        }
        XCTAssertEqual(String(decoding: combined, as: UTF8.self), "first\nsecond\n")
    }

    func testReadabilityPipeReaderCancellationStopsBlockedRead() async throws {
        let pipe = Pipe()
        let reader = ReadabilityPipeReader()
        let task = Task {
            for try await _ in reader.chunks(from: pipe.fileHandleForReading) {}
        }

        try await Task.sleep(nanoseconds: 50_000_000)
        task.cancel()
        _ = await task.result

        try pipe.fileHandleForWriting.close()
    }
}
