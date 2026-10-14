//
// A CAN bus with acknowledgment, bit-rate checking and a harness tap.
//
// Renode's CANHub forwards frames between attached controllers. This bus adds
// what a physical bus decides:
//  - Acknowledgment: a frame completes only if some other node acknowledges it
//    (a Jig controller in an active mode, any other Renode CAN model, or
//    the harness node). With no acknowledger the transmitter sees an ACK error
//    and keeps retransmitting, as on hardware.
//  - Bit timing: a Jig controller whose configured bit rate differs from
//    the bus's nominal rate by more than the tolerance produces no valid frames:
//    nobody receives them and nobody acknowledges them.
//  - A harness node: every frame on the bus is recorded with its virtual time
//    for Read; Write and WriteFrames inject frames as if from an external CAN
//    adapter.
// A frame a machine sends reaches the other machines through
// HandleTimeDomainEvent at the sender's virtual time, as CANHub delivers it.
// The harness writes while the emulation is paused between steps. Its frames
// queue in the adapter and go out back to back at the bus bit rate, one frame
// every (47 + 8n) or (67 + 8n) bit times as the controllers count them, so a
// burst of fragments reaches a controller at line rate instead of all at once
// (the Zynq CAN RX FIFO holds 64 frames).
//  - Line rate for every controller: a Jig controller times its own
//    frames and reports each one as it finishes. Other Renode CAN models (the
//    STM32 bxCAN, for one) raise FrameSent the moment firmware fills a mailbox;
//    the bus queues those frames per controller and puts them on the wire the
//    same way as the harness's, timed by the sender's machine. Without this a
//    27-fragment CSP packet would reach the receiver in a single instant.
//    Frames from different senders are not arbitrated against each other.
//
//   emulation CreateCanBus "can" 1000000
//   connector Connect sysbus.can0 can
//   can Write 0x1C00A00 "0102" true
//   can WriteFrames "1C00A00:E:0102 1C00A01:E:03"
//   can Read
//
// As an IHarnessPort the bus reports the frames seen during each harness step
// as one line: "C <name> <frames>", the frames in Read's format.
//
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.CAN;
using Antmicro.Renode.Exceptions;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.CAN;
using Antmicro.Renode.Time;
using Antmicro.Renode.Utilities;

namespace Antmicro.Renode.Peripherals.Jig.Harness
{
    /// <summary>Implemented by CAN controllers that report acknowledgment and bit timing.</summary>
    public interface IJigCanNode : ICAN
    {
        bool Acknowledges { get; }

        double BitRate { get; }

        void AttachedToBus(CanBus bus);

        void DetachedFromBus(CanBus bus);
    }

    public static class CanBusExtensions
    {
        public static void CreateCanBus(this Emulation emulation, string name, double bitRate = 1000000, double tolerance = 0.01)
        {
            emulation.ExternalsManager.AddExternal(new CanBus(bitRate, tolerance), name);
        }
    }

    public sealed class CanBus : IHarnessPort, IHasOwnLife, IConnectable<ICAN>
    {
        public CanBus(double bitRate, double tolerance)
        {
            NominalBitRate = bitRate;
            Tolerance = tolerance;
        }

        public double NominalBitRate { get; }

        public double Tolerance { get; }

        /// <summary>Whether the harness node acknowledges frames (an attached CAN
        /// adapter in normal mode). Off makes the harness a listen-only observer.</summary>
        public bool HarnessAcknowledges { get; set; } = true;

        /// <summary>Whether the harness records frames for Read.</summary>
        public bool HarnessListening { get; set; } = true;

        public void AttachTo(ICAN node)
        {
            lock(sync)
            {
                if(nodes.Contains(node))
                {
                    throw new RecoverableException("This CAN node is already attached to the bus");
                }
                nodes.Add(node);
                if(node is IJigCanNode jigNode)
                {
                    jigNode.AttachedToBus(this);
                }
                else
                {
                    Action<CANMessageFrame> handler = frame => QueueFromForeign(node, frame);
                    foreignHandlers[node] = handler;
                    foreignQueues[node] = new Queue<CANMessageFrame>();
                    node.FrameSent += handler;
                }
            }
        }

        public void DetachFrom(ICAN node)
        {
            lock(sync)
            {
                if(!nodes.Remove(node))
                {
                    throw new RecoverableException("This CAN node is not attached to the bus");
                }
                if(node is IJigCanNode jigNode)
                {
                    jigNode.DetachedFromBus(this);
                }
                else if(foreignHandlers.TryGetValue(node, out var handler))
                {
                    node.FrameSent -= handler;
                    foreignHandlers.Remove(node);
                    foreignQueues.Remove(node);
                    foreignTransmitting.Remove(node);
                }
            }
        }

        public void Start() => Resume();

        public void Pause()
        {
            lock(sync) started = false;
        }

        public void Resume()
        {
            lock(sync) started = true;
        }

        public bool IsPaused => !started;

        /// <summary>Called by a node when a frame finishes on the wire. Returns
        /// whether any other node acknowledged it.</summary>
        public bool Transmit(ICAN sender, CANMessageFrame frame)
        {
            lock(sync)
            {
                if(sender is IJigCanNode timed && !RateMatches(timed.BitRate))
                {
                    this.Log(LogLevel.Warning, "{0} transmits at {1:F0} bit/s on a {2:F0} bit/s bus: frame lost",
                        sender.GetName(), timed.BitRate, NominalBitRate);
                    return false;
                }
                Record(frame, Describe(sender));
                Deliver(sender, frame);
                return HarnessAcknowledges || nodes.Any(n => n != sender && Acknowledges(n));
            }
        }

        // ------------------------------------------------------------- harness

        /// <summary>Frames seen since the last read: "us:id:flags:data" entries
        /// separated by spaces; flags are E (extended), R (remote), and the
        /// sender after '@'.</summary>
        public string Read()
        {
            lock(sync)
            {
                var builder = new StringBuilder();
                foreach(var line in log)
                {
                    if(builder.Length > 0)
                    {
                        builder.Append(' ');
                    }
                    builder.Append(line);
                }
                log.Clear();
                return builder.ToString();
            }
        }

        /// <summary>Queues a frame from the harness node. Returns "ack" when some
        /// node on the bus acknowledges frames, else "noack".</summary>
        public string Write(ulong id, string hexData, bool extended = true, bool remote = false)
        {
            var frame = BuildFrame(id, ParseHex(hexData ?? ""), extended, remote);
            lock(sync)
            {
                QueueFromHarness(frame);
                return nodes.Any(Acknowledges) ? "ack" : "noack";
            }
        }

        /// <summary>Queues several harness frames in order: space-separated
        /// "IDhex:flags:datahex" entries, flags E (extended) and R (remote).</summary>
        public string WriteFrames(string frames)
        {
            var parsed = new List<CANMessageFrame>();
            foreach(var entry in (frames ?? "").Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries))
            {
                var parts = entry.Split(':');
                if(parts.Length != 3 || !ulong.TryParse(parts[0], NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var id))
                {
                    throw new RecoverableException($"Malformed frame '{entry}' (expected IDhex:flags:datahex)");
                }
                parsed.Add(BuildFrame(id, ParseHex(parts[2]), parts[1].Contains("E"), parts[1].Contains("R")));
            }
            lock(sync)
            {
                foreach(var frame in parsed)
                {
                    QueueFromHarness(frame);
                }
                return nodes.Any(Acknowledges) ? "ack" : "noack";
            }
        }

        /// <summary>Frames the harness has queued that are not on the wire yet.</summary>
        public int HarnessPending
        {
            get
            {
                lock(sync)
                {
                    return harnessQueue.Count;
                }
            }
        }

        public void Collect(string name, StringBuilder output)
        {
            lock(sync)
            {
                if(log.Count == 0)
                {
                    return;
                }
                output.Append("C ").Append(name);
                foreach(var line in log)
                {
                    output.Append(' ').Append(line);
                }
                output.Append('\n');
                log.Clear();
            }
        }

        // ------------------------------------------------------------- helpers

        private static CANMessageFrame BuildFrame(ulong id, byte[] data, bool extended, bool remote)
        {
            if(data.Length > 8)
            {
                throw new RecoverableException("Classical CAN frames carry at most 8 data bytes");
            }
            if(id >= (extended ? 1UL << 29 : 1UL << 11))
            {
                throw new RecoverableException($"Identifier 0x{id:X} does not fit a {(extended ? 29 : 11)}-bit field");
            }
            return new CANMessageFrame((uint)id, data, extended, remote);
        }

        private void QueueFromHarness(CANMessageFrame frame)
        {
            harnessQueue.Enqueue(frame);
            StartHarnessTransmission();
        }

        private ulong FrameNanoseconds(CANMessageFrame frame)
        {
            var bits = (frame.ExtendedFormat ? 67 : 47) + (frame.RemoteFrame ? 0 : 8 * frame.Data.Length);
            return (ulong)Math.Max(1.0, Math.Round(bits * 1e9 / NominalBitRate));
        }

        // Runs in the sender's machine thread when its firmware fills a mailbox.
        private void QueueFromForeign(ICAN node, CANMessageFrame frame)
        {
            lock(sync)
            {
                if(!foreignQueues.TryGetValue(node, out var queue))
                {
                    return; // detached meanwhile
                }
                queue.Enqueue(frame);
                StartForeignTransmission(node);
            }
        }

        // Called with sync held. The sender's machine keeps the time; a frame
        // completes one frame time after the previous one.
        private void StartForeignTransmission(ICAN node)
        {
            if(foreignTransmitting.Contains(node) || !foreignQueues.TryGetValue(node, out var queue) || queue.Count == 0)
            {
                return;
            }
            var clock = node.GetMachine();
            if(clock == null)
            {
                while(queue.Count > 0)
                {
                    Transmit(node, queue.Dequeue());
                }
                return;
            }
            foreignTransmitting.Add(node);
            clock.ScheduleAction(TimeInterval.FromNanoseconds(FrameNanoseconds(queue.Peek())), _ =>
            {
                lock(sync)
                {
                    foreignTransmitting.Remove(node);
                    if(!foreignQueues.TryGetValue(node, out var pending) || pending.Count == 0)
                    {
                        return; // detached meanwhile
                    }
                    Transmit(node, pending.Dequeue());
                    StartForeignTransmission(node);
                }
            }, "can-foreign-tx");
        }

        // Called with sync held. The first attached machine keeps the adapter's
        // time; a frame completes one frame time after the previous one.
        private void StartHarnessTransmission()
        {
            if(harnessTransmitting || harnessQueue.Count == 0)
            {
                return;
            }
            var clock = nodes.Select(n => n.GetMachine()).FirstOrDefault(m => m != null);
            if(clock == null)
            {
                // Nobody on the bus: the frames go nowhere, but they were sent.
                while(harnessQueue.Count > 0)
                {
                    Record(harnessQueue.Dequeue(), "harness");
                }
                return;
            }
            var nanoseconds = FrameNanoseconds(harnessQueue.Peek());
            harnessTransmitting = true;
            clock.ScheduleAction(TimeInterval.FromNanoseconds(nanoseconds), _ =>
            {
                lock(sync)
                {
                    harnessTransmitting = false;
                    if(harnessQueue.Count == 0)
                    {
                        return; // cleared meanwhile
                    }
                    var done = harnessQueue.Dequeue();
                    Record(done, "harness", clock);
                    DeliverFromHarness(done, clock);
                    StartHarnessTransmission();
                }
            }, "can-harness-tx");
        }

        private bool RateMatches(double rate)
        {
            return rate > 0 && Math.Abs(rate - NominalBitRate) <= Tolerance * NominalBitRate;
        }

        private bool Acknowledges(ICAN node)
        {
            if(node is IJigCanNode jigNode)
            {
                return jigNode.Acknowledges && RateMatches(jigNode.BitRate);
            }
            return true;
        }

        // Runs in the clock machine's thread when a harness frame finishes.
        private void DeliverFromHarness(CANMessageFrame frame, IMachine clock)
        {
            var stamp = clock.ElapsedVirtualTime;
            foreach(var node in nodes.Where(Decodes))
            {
                var machine = node.GetMachine();
                if(machine == clock)
                {
                    node.OnFrameReceived(frame);
                }
                else
                {
                    machine.HandleTimeDomainEvent(node.OnFrameReceived, frame, stamp);
                }
            }
        }

        private bool Decodes(ICAN node)
        {
            // A node sampling at the wrong rate decodes nothing.
            return !(node is IJigCanNode timed) || RateMatches(timed.BitRate);
        }

        private void Deliver(ICAN sender, CANMessageFrame frame)
        {
            var stamp = TimeDomainsManager.Instance.GetEffectiveVirtualTimeStamp();
            foreach(var node in nodes)
            {
                if(node == sender)
                {
                    continue;
                }
                if(node is IJigCanNode timed && !RateMatches(timed.BitRate))
                {
                    continue; // a node sampling at the wrong rate decodes nothing
                }
                node.GetMachine().HandleTimeDomainEvent(node.OnFrameReceived, frame, stamp);
            }
        }

        private void Record(CANMessageFrame frame, string sender, IMachine clock = null)
        {
            if(!HarnessListening)
            {
                return;
            }
            TimeInterval stamp;
            if(clock != null)
            {
                stamp = clock.ElapsedVirtualTime.TimeElapsed;
            }
            else if(!TimeDomainsManager.Instance.TryGetVirtualTimeStamp(out var threadStamp))
            {
                stamp = EmulationManager.Instance.CurrentEmulation.MasterTimeSource.ElapsedVirtualTime;
            }
            else
            {
                stamp = threadStamp.TimeElapsed;
            }
            var flags = (frame.ExtendedFormat ? "E" : "") + (frame.RemoteFrame ? "R" : "");
            var data = new StringBuilder(frame.Data.Length * 2);
            foreach(var b in frame.Data)
            {
                data.Append(b.ToString("X2", CultureInfo.InvariantCulture));
            }
            log.Add(string.Format(CultureInfo.InvariantCulture, "{0}:{1:X}:{2}@{3}:{4}",
                (ulong)Math.Round(stamp.TotalMicroseconds), frame.Id, flags, sender, data));
        }

        private static string Describe(ICAN node)
        {
            return node.GetName().Replace(':', '.').Replace(' ', '_');
        }

        private static byte[] ParseHex(string hex)
        {
            if(hex.Length % 2 != 0)
            {
                throw new RecoverableException("Frame data must be an even-length hex string");
            }
            var result = new byte[hex.Length / 2];
            for(var i = 0; i < result.Length; i++)
            {
                if(!byte.TryParse(hex.AsSpan(2 * i, 2), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out result[i]))
                {
                    throw new RecoverableException($"Invalid hex in frame data at position {2 * i}");
                }
            }
            return result;
        }

        private readonly object sync = new object();
        private readonly List<ICAN> nodes = new List<ICAN>();
        private readonly Dictionary<ICAN, Action<CANMessageFrame>> foreignHandlers = new Dictionary<ICAN, Action<CANMessageFrame>>();
        private readonly Dictionary<ICAN, Queue<CANMessageFrame>> foreignQueues = new Dictionary<ICAN, Queue<CANMessageFrame>>();
        private readonly HashSet<ICAN> foreignTransmitting = new HashSet<ICAN>();
        private readonly List<string> log = new List<string>();
        private readonly Queue<CANMessageFrame> harnessQueue = new Queue<CANMessageFrame>();
        private bool harnessTransmitting;
        private bool started;
    }
}
