//
// Test fixture: GPIO lines a test reads and drives from the monitor, standing
// in for bench equipment outside the board.
//
// Inputs: a model's output wired here ("slcr: [0-3] -> probe@[0-3]") is
// recorded per line: its level, and how many rising and falling edges it made.
// A pulse (GPIO.Blink) counts one rise and one fall.
// Outputs: Connections[n] goes to a model's input ("probe: 0 -> gic@0") and
// follows Drive and Pulse.
//
// A machine reset leaves the record and the driven levels alone: the probe is
// not part of the board. Clear empties the record.
//
//   probe Level 3         True while input 3 is high
//   probe Rises 3         rising edges seen on input 3
//   probe Drive 0 true    output 0 high
//
using System.Collections.Generic;
using Antmicro.Renode.Core;
using Antmicro.Renode.Exceptions;

namespace Antmicro.Renode.Peripherals.JigFixtures
{
    public sealed class GpioProbe : IGPIOReceiver, INumberedGPIOOutput
    {
        public GpioProbe(IMachine machine, int lines = 64)
        {
            if(lines < 1)
            {
                throw new ConstructionException("A probe needs at least one line");
            }
            this.lines = lines;
            levels = new bool[lines];
            rises = new int[lines];
            falls = new int[lines];
            var outputs = new Dictionary<int, IGPIO>();
            for(var i = 0; i < lines; i++)
            {
                outputs[i] = new GPIO();
            }
            Connections = outputs;
        }

        public IReadOnlyDictionary<int, IGPIO> Connections { get; }

        public void Reset()
        {
        }

        public void OnGPIO(int number, bool value)
        {
            Check(number);
            lock(sync)
            {
                if(value && !levels[number])
                {
                    rises[number]++;
                }
                else if(!value && levels[number])
                {
                    falls[number]++;
                }
                levels[number] = value;
            }
        }

        public bool Level(int line)
        {
            Check(line);
            lock(sync)
            {
                return levels[line];
            }
        }

        public int Rises(int line)
        {
            Check(line);
            lock(sync)
            {
                return rises[line];
            }
        }

        public int Falls(int line)
        {
            Check(line);
            lock(sync)
            {
                return falls[line];
            }
        }

        public void Clear()
        {
            lock(sync)
            {
                for(var i = 0; i < lines; i++)
                {
                    rises[i] = 0;
                    falls[i] = 0;
                }
            }
        }

        public void Drive(int line, bool value)
        {
            Check(line);
            Connections[line].Set(value);
        }

        public void Pulse(int line)
        {
            Check(line);
            Connections[line].Blink();
        }

        private void Check(int line)
        {
            if(line < 0 || line >= lines)
            {
                throw new RecoverableException($"Probe line {line} does not exist (0-{lines - 1})");
            }
        }

        private readonly object sync = new object();
        private readonly int lines;
        private readonly bool[] levels;
        private readonly int[] rises;
        private readonly int[] falls;
    }
}
