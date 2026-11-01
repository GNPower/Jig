//
// Contract for an analog-to-digital converter whose inputs a test drives
// (a board file's sensor lines: sensor NAME: adc.channel[N]).
//
namespace Antmicro.Renode.Peripherals.Jig.Contracts
{
    /// <summary>A converter with input channels 0 to <see cref="ChannelCount"/> - 1;
    /// any other channel number throws RecoverableException. Codes are the
    /// unsigned values the converter reports, 0 to its full scale (4095 for 12
    /// bits). The input voltages come from outside the device: Reset and
    /// PowerCycle keep them. A channel never set reports CodeFor(0).</summary>
    public interface IAnalogInputs : IPeripheral
    {
        /// <summary>Number of input channels.</summary>
        int ChannelCount { get; }

        /// <summary>Sets the voltage at input <paramref name="channel"/>, in volts
        /// at the device pin. Conversions of that channel from now on report
        /// CodeFor(<paramref name="volts"/>).</summary>
        void SetChannelVoltage(int channel, double volts);

        /// <summary>The code a conversion of <paramref name="channel"/> reports
        /// now.</summary>
        int GetChannelCode(int channel);

        /// <summary>The code this converter reports for an input of
        /// <paramref name="volts"/>: its transfer function, rounded to the nearest
        /// code and limited to 0..full scale. No side effects.</summary>
        int CodeFor(double volts);
    }
}
