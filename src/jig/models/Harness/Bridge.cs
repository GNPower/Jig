//
// Jig's control channel into Renode, in place of the Robot Framework server.
//
// The bridge listens on 127.0.0.1 only, on a port the operating system picks,
// and serves one authenticated client. Jig starts it as the last start-up
// command, once its models are compiled:
//
//     emulation StartJigBridge "bridge.json"
//
// The command writes the file (relative paths resolve against Renode's working
// directory; owner-only on Linux and macOS):
//
//     {"protocol": 1, "host": "127.0.0.1", "port": 50123, "token": "<64 hex digits>", "pid": 4242, ...}
//
// and then executes requests on the thread that ran it, until the client
// quits or disconnects. That thread is the monitor's own, so requests run one
// at a time in arrival order, as typed commands would.
//
// A request or reply is a frame: a 4-byte big-endian length, then a UTF-8
// JSON object. Every request carries the token, compared in constant time; a
// wrong token gets an error reply and the connection closes. Operations:
//
//     hello    {protocol}            -> {protocol, pid, version, runtime, infrastructure}
//     command  {command, machine?}   -> the monitor's output (a string)
//     python   {code, machine?}      -> the value of the IronPython code
//     compile  {files}               -> {seconds}; compiler errors as the error text
//     quit                           -> null; Renode exits after the reply
//
// "command" and "python" go through the same Renode internals as the Robot
// keywords ExecuteCommand and ExecutePython (Monitor.Parse with a
// CommandInteractionWrapper, Monitor.ExecutePythonCommand), with the same
// error texts; "compile" goes through Monitor.TryCompilePlugin, as the
// monitor's include command does. Output a command prints goes into its reply
// only, not to Renode's console. When the authenticated client disconnects
// without "quit", or none connects within a minute, Renode exits, so an
// emulation never outlives the process that drives it.
//
using System;
using System.Collections;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;

using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.UserInterface;

using RenodeMonitor = Antmicro.Renode.UserInterface.Monitor;

namespace Antmicro.Renode.Peripherals.Jig.Harness
{
    public static class JigBridgeExtensions
    {
        /// <summary>Starts the bridge and serves requests until the client
        /// quits; see Bridge.cs.</summary>
        public static void StartJigBridge(this Emulation emulation, string infoPath)
        {
            JigBridge.Run(infoPath);
        }

        /// <summary>The bridge protocol these models implement.</summary>
        public static int JigProtocolVersion(this Emulation emulation)
        {
            return JigBridge.ProtocolVersion;
        }
    }

    public sealed class JigBridge
    {
        public const int ProtocolVersion = 1;

        /// <summary>Set by Jig's start-up script: how long the start-up
        /// compile of Jig's models took, reported in the info file.</summary>
        public const string StartupCompileVariable = "JIG_STARTUP_COMPILE_MS";

        public static void Run(string infoPath)
        {
            JigBridge bridge;
            lock(StartLock)
            {
                if(started)
                {
                    throw new RecoverableException("The Jig bridge has already run in this Renode process");
                }
                started = true;
                bridge = new JigBridge(Path.GetFullPath(infoPath));
            }
            bridge.Listen();
            bridge.Serve();
        }

        private JigBridge(string infoPath)
        {
            this.infoPath = infoPath;
            monitor = ObjectCreator.Instance.GetSurrogate<RenodeMonitor>();
            if(monitor == null)
            {
                throw new RecoverableException("The Jig bridge needs Renode's monitor");
            }
            token = RandomNumberGenerator.GetBytes(TokenBytes);
            tokenText = Convert.ToHexString(token).ToLowerInvariant();
            tokenUtf8 = Encoding.UTF8.GetBytes(tokenText);
        }

        private void Listen()
        {
            listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            port = ((IPEndPoint)listener.LocalEndpoint).Port;
            WriteInfo();
            new Thread(AcceptLoop) { IsBackground = true, Name = "Jig bridge listener" }.Start();
            noClientTimer = new Timer(_ => OnNoClient(), null, NoClientTimeoutMilliseconds, Timeout.Infinite);
            Logger.Log(LogLevel.Info, "Jig bridge: listening on 127.0.0.1:{0} (protocol {1})", port, ProtocolVersion);
        }

        // Runs requests on this thread, the one that started the bridge.
        private void Serve()
        {
            while(true)
            {
                Job job;
                try
                {
                    job = jobs.Take();
                }
                catch(InvalidOperationException)
                {
                    return; // CompleteAdding: the bridge is closing
                }
                job.Reply = Execute(job.Request);
                job.Done.Set();
            }
        }

        private void WriteInfo()
        {
            var startupCompile = 0L;
            long.TryParse(Environment.GetEnvironmentVariable(StartupCompileVariable), NumberStyles.Integer, CultureInfo.InvariantCulture, out startupCompile);
            var buffer = new MemoryStream();
            using(var writer = new Utf8JsonWriter(buffer, new JsonWriterOptions { Indented = true }))
            {
                writer.WriteStartObject();
                writer.WriteNumber("protocol", ProtocolVersion);
                writer.WriteString("host", "127.0.0.1");
                writer.WriteNumber("port", port);
                writer.WriteString("token", tokenText);
                writer.WriteNumber("pid", Environment.ProcessId);
                writer.WriteNumber("startup_compile_ms", startupCompile);
                writer.WriteEndObject();
            }
            var directory = Path.GetDirectoryName(infoPath);
            if(!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }
            var temporary = infoPath + ".tmp";
            File.Delete(temporary);
            var options = new FileStreamOptions { Mode = FileMode.CreateNew, Access = FileAccess.Write, Share = FileShare.None };
            if(!OperatingSystem.IsWindows())
            {
                options.UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite;
            }
            using(var file = new FileStream(temporary, options))
            {
                file.Write(buffer.ToArray());
            }
            File.Move(temporary, infoPath, true);
        }

        private void AcceptLoop()
        {
            while(!closing)
            {
                TcpClient client;
                try
                {
                    client = listener.AcceptTcpClient();
                }
                catch(SocketException)
                {
                    return; // the listener stopped
                }
                catch(ObjectDisposedException)
                {
                    return;
                }
                new Thread(() => Connection(client)) { IsBackground = true, Name = "Jig bridge connection" }.Start();
            }
        }

        private void Connection(TcpClient client)
        {
            var authenticated = false;
            try
            {
                client.NoDelay = true;
                client.ReceiveTimeout = HandshakeTimeoutMilliseconds;
                var stream = client.GetStream();
                while(!closing)
                {
                    var body = ReadFrame(stream, authenticated ? MaxFrameBytes : MaxHandshakeFrameBytes);
                    if(body == null)
                    {
                        break; // the client closed the connection
                    }
                    var request = Request.Parse(body);
                    if(request == null)
                    {
                        WriteFrame(stream, ErrorReply(0, "request", "a request must be a JSON object with an op"));
                        break;
                    }
                    if(!TokenMatches(request.Token))
                    {
                        WriteFrame(stream, ErrorReply(request.Id, "auth", "invalid token"));
                        break;
                    }
                    if(!authenticated)
                    {
                        lock(clientLock)
                        {
                            if(hasClient)
                            {
                                WriteFrame(stream, ErrorReply(request.Id, "busy", "another client is connected to this bridge"));
                                break;
                            }
                            hasClient = true;
                        }
                        authenticated = true;
                        noClientTimer?.Dispose();
                        client.ReceiveTimeout = 0;
                    }
                    var job = new Job(request);
                    try
                    {
                        jobs.Add(job);
                    }
                    catch(InvalidOperationException)
                    {
                        break; // closing
                    }
                    job.Done.Wait();
                    WriteFrame(stream, job.Reply);
                    if(request.Op == "quit")
                    {
                        Close("the client asked Renode to quit");
                        return;
                    }
                }
            }
            catch(IOException)
            {
                // a timeout, a reset or an oversized frame: drop the connection
            }
            catch(SocketException)
            {
            }
            catch(ObjectDisposedException)
            {
            }
            finally
            {
                client.Close();
            }
            if(authenticated && !closing)
            {
                Close("the client disconnected");
            }
        }

        private void OnNoClient()
        {
            lock(clientLock)
            {
                if(hasClient)
                {
                    return;
                }
            }
            Close($"no client connected within {NoClientTimeoutMilliseconds / 1000} s");
        }

        private void Close(string reason)
        {
            lock(StartLock)
            {
                if(closing)
                {
                    return;
                }
                closing = true;
            }
            Logger.Log(LogLevel.Info, "Jig bridge: {0}; Renode exits", reason);
            noClientTimer?.Dispose();
            try
            {
                listener.Stop();
            }
            catch(SocketException)
            {
            }
            jobs.CompleteAdding();
            Emulator.Exit();
        }

        private bool TokenMatches(string given)
        {
            var bytes = Encoding.UTF8.GetBytes(given ?? string.Empty);
            return CryptographicOperations.FixedTimeEquals(bytes, tokenUtf8);
        }

        // ------------------------------------------------------------ requests

        private byte[] Execute(Request request)
        {
            try
            {
                switch(request.Op)
                {
                case "hello":
                    return Reply(request.Id, writer => Hello(writer));
                case "command":
                {
                    var output = ExecuteCommand(request.Text("command"), request.Text("machine"));
                    return Reply(request.Id, writer => writer.WriteStringValue(output));
                }
                case "python":
                {
                    var value = ExecutePython(request.Text("code"), request.Text("machine"));
                    return Reply(request.Id, writer => WriteValue(writer, value, 0));
                }
                case "compile":
                {
                    var seconds = Compile(request.Texts("files"));
                    return Reply(request.Id, writer =>
                    {
                        writer.WriteStartObject();
                        writer.WriteNumber("seconds", seconds);
                        writer.WriteEndObject();
                    });
                }
                case "quit":
                    return Reply(request.Id, writer => writer.WriteNullValue());
                default:
                    return ErrorReply(request.Id, "request", $"unknown op '{request.Op}'");
                }
            }
            catch(BridgeException e)
            {
                return ErrorReply(request.Id, e.Kind, e.Message);
            }
            catch(KeywordException e)
            {
                return ErrorReply(request.Id, request.Op == "python" ? "python" : "command", Describe(e));
            }
            catch(Exception e)
            {
                return ErrorReply(request.Id, request.Op == "python" ? "python" : "internal", Describe(e));
            }
        }

        private void Hello(Utf8JsonWriter writer)
        {
            writer.WriteStartObject();
            writer.WriteNumber("protocol", ProtocolVersion);
            writer.WriteNumber("pid", Environment.ProcessId);
            writer.WriteString("version", EmulationManager.Instance.VersionString);
            writer.WriteString("runtime", ".NET " + Environment.Version.ToString());
            writer.WriteString("infrastructure", typeof(EmulationManager).Assembly.Location);
            writer.WriteEndObject();
        }

        // As the Robot keyword ExecuteCommand (Renode 1.17.0, RenodeKeywords.cs).
        private string ExecuteCommand(string command, string machine)
        {
            if(command == null)
            {
                throw new BridgeException("request", "command needs a 'command' string");
            }
            var interaction = Interaction();
            interaction.Clear();
            SelectMachine(machine);

            if(!monitor.Parse(command))
            {
                throw new KeywordException($"Could not execute command '{command}': {interaction.GetError()}");
            }

            var error = interaction.GetError();
            if(!string.IsNullOrEmpty(error))
            {
                throw new KeywordException($"There was an error when executing command '{command}': {error}");
            }

            return EscapeSequences.Replace(interaction.GetContents(), "");
        }

        // As the Robot keyword ExecutePython (Renode 1.17.0, RenodeKeywords.cs).
        private object ExecutePython(string code, string machine)
        {
            if(code == null)
            {
                throw new BridgeException("request", "python needs a 'code' string");
            }
            SelectMachine(machine);
            try
            {
                return monitor.ExecutePythonCommand(code);
            }
            catch(RecoverableException e) when(e.InnerException != null)
            {
                ExceptionDispatchInfo.Capture(e.InnerException).Throw();
                throw;
            }
        }

        private double Compile(string[] files)
        {
            if(files == null || files.Length == 0)
            {
                throw new BridgeException("request", "compile needs a non-empty 'files' list");
            }
            var eater = new CommandInteractionEater();
            var watch = Stopwatch.StartNew();
            var compiled = monitor.TryCompilePlugin(files, eater);
            var seconds = watch.Elapsed.TotalSeconds;
            if(!compiled || eater.HasError)
            {
                var error = eater.GetError();
                throw new BridgeException("compile", string.IsNullOrWhiteSpace(error) ? "Renode compiled the files but could not load their types" : error);
            }
            return seconds;
        }

        // The Robot keywords wrap the shell's interaction; the bridge wraps one
        // that discards, so command output reaches the reply and nothing else.
        private CommandInteractionWrapper Interaction()
        {
            if(!(monitor.Interaction is CommandInteractionWrapper wrapper))
            {
                wrapper = new CommandInteractionWrapper(new DummyCommandInteraction());
                monitor.Interaction = wrapper;
            }
            return wrapper;
        }

        private void SelectMachine(string machine)
        {
            if(string.IsNullOrWhiteSpace(machine))
            {
                return;
            }
            if(!EmulationManager.Instance.CurrentEmulation.TryGetMachineByName(machine, out var found))
            {
                throw new KeywordException($"Could not find machine named {machine} in the emulation");
            }
            monitor.Machine = found;
        }

        // ----------------------------------------------------------- encoding

        private static byte[] Reply(long id, Action<Utf8JsonWriter> result)
        {
            var buffer = new MemoryStream();
            using(var writer = new Utf8JsonWriter(buffer))
            {
                writer.WriteStartObject();
                writer.WriteNumber("id", id);
                writer.WriteBoolean("ok", true);
                writer.WritePropertyName("result");
                result(writer);
                writer.WriteEndObject();
            }
            return buffer.ToArray();
        }

        private static byte[] ErrorReply(long id, string kind, string message)
        {
            var buffer = new MemoryStream();
            using(var writer = new Utf8JsonWriter(buffer))
            {
                writer.WriteStartObject();
                writer.WriteNumber("id", id);
                writer.WriteBoolean("ok", false);
                writer.WriteString("kind", kind);
                writer.WriteString("error", message ?? string.Empty);
                writer.WriteEndObject();
            }
            return buffer.ToArray();
        }

        // The value of IronPython code as JSON: numbers, strings, booleans,
        // lists and dictionaries as themselves, anything else as its text.
        private static void WriteValue(Utf8JsonWriter writer, object value, int depth)
        {
            switch(value)
            {
            case null:
                writer.WriteNullValue();
                return;
            case string text:
                writer.WriteStringValue(text);
                return;
            case bool flag:
                writer.WriteBooleanValue(flag);
                return;
            case char character:
                writer.WriteStringValue(character.ToString());
                return;
            case byte or sbyte or short or ushort or int or uint or long:
                writer.WriteNumberValue(Convert.ToInt64(value, CultureInfo.InvariantCulture));
                return;
            case ulong unsigned:
                writer.WriteNumberValue(unsigned);
                return;
            case float or double:
                var real = Convert.ToDouble(value, CultureInfo.InvariantCulture);
                if(double.IsFinite(real))
                {
                    writer.WriteNumberValue(real);
                }
                else
                {
                    writer.WriteStringValue(real.ToString(CultureInfo.InvariantCulture));
                }
                return;
            case decimal exact:
                writer.WriteNumberValue(exact);
                return;
            }
            if(value.GetType().FullName == "System.Numerics.BigInteger")
            {
                // Python's long: JSON numbers have no size limit.
                writer.WriteRawValue(Convert.ToString(value, CultureInfo.InvariantCulture));
                return;
            }
            if(depth < MaxValueDepth && value is IDictionary dictionary)
            {
                writer.WriteStartObject();
                foreach(DictionaryEntry entry in dictionary)
                {
                    writer.WritePropertyName(Convert.ToString(entry.Key, CultureInfo.InvariantCulture) ?? string.Empty);
                    WriteValue(writer, entry.Value, depth + 1);
                }
                writer.WriteEndObject();
                return;
            }
            if(depth < MaxValueDepth && value is IEnumerable sequence)
            {
                writer.WriteStartArray();
                foreach(var item in sequence)
                {
                    WriteValue(writer, item, depth + 1);
                }
                writer.WriteEndArray();
                return;
            }
            writer.WriteStringValue(Convert.ToString(value, CultureInfo.InvariantCulture));
        }

        // As XmlRpcServer.BuildRecursiveValueFromException: one line per
        // exception in the chain, TargetInvocationException left out.
        private static string Describe(Exception exception)
        {
            var text = new StringBuilder();
            for(var e = exception; e != null; e = e.InnerException)
            {
                if(!(e is TargetInvocationException))
                {
                    text.AppendFormat("{0}: {1}\n", e.GetType().Name, e.Message);
                }
            }
            return text.ToString();
        }

        private static byte[] ReadFrame(Stream stream, int limit)
        {
            var header = new byte[4];
            if(!ReadExactly(stream, header, true))
            {
                return null;
            }
            var length = ((long)header[0] << 24) | ((long)header[1] << 16) | ((long)header[2] << 8) | header[3];
            if(length > limit)
            {
                throw new IOException($"a frame of {length} bytes exceeds the limit of {limit}");
            }
            var body = new byte[length];
            ReadExactly(stream, body, false);
            return body;
        }

        private static bool ReadExactly(Stream stream, byte[] buffer, bool endAllowed)
        {
            var offset = 0;
            while(offset < buffer.Length)
            {
                var count = stream.Read(buffer, offset, buffer.Length - offset);
                if(count == 0)
                {
                    if(endAllowed && offset == 0)
                    {
                        return false;
                    }
                    throw new IOException("the connection closed inside a frame");
                }
                offset += count;
            }
            return true;
        }

        private static void WriteFrame(Stream stream, byte[] body)
        {
            var frame = new byte[body.Length + 4];
            frame[0] = (byte)(body.Length >> 24);
            frame[1] = (byte)(body.Length >> 16);
            frame[2] = (byte)(body.Length >> 8);
            frame[3] = (byte)body.Length;
            Buffer.BlockCopy(body, 0, frame, 4, body.Length);
            stream.Write(frame, 0, frame.Length);
            stream.Flush();
        }

        private TcpListener listener;
        private Timer noClientTimer;
        private int port;
        private volatile bool closing;
        private bool hasClient;

        private readonly string infoPath;
        private readonly RenodeMonitor monitor;
        private readonly byte[] token;
        private readonly string tokenText;
        private readonly byte[] tokenUtf8;
        private readonly object clientLock = new object();
        private readonly BlockingCollection<Job> jobs = new BlockingCollection<Job>();

        private static bool started;

        private static readonly object StartLock = new object();
        // The Robot server strips terminal escape sequences from string results.
        private static readonly Regex EscapeSequences = new Regex("(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]", RegexOptions.Compiled);

        private const int TokenBytes = 32;
        private const int HandshakeTimeoutMilliseconds = 10000;
        private const int NoClientTimeoutMilliseconds = 60000;
        private const int MaxHandshakeFrameBytes = 64 * 1024;
        private const int MaxFrameBytes = 256 * 1024 * 1024;
        private const int MaxValueDepth = 32;

        private sealed class Job
        {
            public Job(Request request)
            {
                Request = request;
            }

            public Request Request { get; }

            public byte[] Reply { get; set; }

            public ManualResetEventSlim Done { get; } = new ManualResetEventSlim(false);
        }

        private sealed class Request
        {
            public static Request Parse(byte[] body)
            {
                try
                {
                    using(var document = JsonDocument.Parse(new ReadOnlyMemory<byte>(body)))
                    {
                        var root = document.RootElement;
                        if(root.ValueKind != JsonValueKind.Object
                            || !root.TryGetProperty("op", out var op) || op.ValueKind != JsonValueKind.String)
                        {
                            return null;
                        }
                        return new Request(root.Clone());
                    }
                }
                catch(JsonException)
                {
                    return null;
                }
            }

            private Request(JsonElement root)
            {
                this.root = root;
                Op = root.GetProperty("op").GetString();
                Id = root.TryGetProperty("id", out var id) && id.ValueKind == JsonValueKind.Number && id.TryGetInt64(out var value) ? value : 0;
                Token = Text("token");
            }

            public string Op { get; }

            public long Id { get; }

            public string Token { get; }

            public string Text(string name)
            {
                return root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;
            }

            public string[] Texts(string name)
            {
                if(!root.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.Array)
                {
                    return null;
                }
                var items = new string[value.GetArrayLength()];
                var index = 0;
                foreach(var item in value.EnumerateArray())
                {
                    if(item.ValueKind != JsonValueKind.String)
                    {
                        return null;
                    }
                    items[index++] = item.GetString();
                }
                return items;
            }

            private readonly JsonElement root;
        }

        private sealed class BridgeException : Exception
        {
            public BridgeException(string kind, string message) : base(message)
            {
                Kind = kind;
            }

            public string Kind { get; }
        }

        // Named as the Robot server's exception, so an error reads as it did
        // through ExecuteCommand ("KeywordException: Could not execute ...").
        private sealed class KeywordException : Exception
        {
            public KeywordException(string message) : base(message)
            {
            }
        }
    }
}
