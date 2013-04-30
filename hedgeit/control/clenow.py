'''
hedgeit.analyzer.istrategy

Contains:
  class ClenowController
'''
from hedgeit.analyzer.istrategy import InstrumentedStrategy
from hedgeit.feeds.feed import Feed
from hedgeit.feeds.db import InstrumentDb
from hedgeit.feeds.multifeed import MultiFeed
from hedgeit.strategy.clenow import ClenowBreakoutStrategy
from hedgeit.strategy.clenow import ClenowBreakoutNoIntraDayStopStrategy
from hedgeit.strategy.macross import MACrossStrategy
from hedgeit.analyzer.drawdown import DrawDown
from hedgeit.broker.brokers import BacktestingFuturesBroker
from hedgeit.broker.commissions import FuturesCommission
import numpy
        
class ClenowController(object):
    def __init__(self, sectorMap, positionsFile, equityFile, returnsFile, \
                 cash = 1000000, riskFactor = 0.002, period=50, stop=3.0, \
                 tradeStart=None, intraDayStop = True, summaryFile=None,
                 modelType=None):

        self._runGroups = {}
        self._startingCash = cash    

        self._db = InstrumentDb.Instance()
        self._feed = MultiFeed()
        self._broker = BacktestingFuturesBroker(cash, self._feed, commission=FuturesCommission(2.50)) 
        for sec in sectorMap:
            for sym in sectorMap[sec]:
                self._feed.register_feed(Feed(self._db.get(sym)))
        
            if not modelType or modelType == 'breakout':
                if intraDayStop:
                    strategy = ClenowBreakoutStrategy(self._feed, 
                                                      symbols=sectorMap[sec], 
                                                      broker=self._broker, 
                                                      cash=cash, 
                                                      riskFactor=riskFactor, 
                                                      period=period, 
                                                      stop=stop, 
                                                      tradeStart=tradeStart)
                else:
                    strategy = ClenowBreakoutNoIntraDayStopStrategy(self._feed, 
                                                                    symbols=sectorMap[sec], 
                                                                    broker=self._broker, 
                                                                    cash=cash, 
                                                                    riskFactor=riskFactor, 
                                                                    period=period, 
                                                                    stop=stop, 
                                                                    tradeStart=tradeStart)
            elif modelType == 'macross':
                strategy = MACrossStrategy(self._feed, 
                                           symbols=sectorMap[sec], 
                                           broker=self._broker, 
                                           cash=cash, 
                                           riskFactor=riskFactor, 
                                           shortPeriod=period / 10,
                                           longPeriod=period, 
                                           stop=stop, 
                                           tradeStart=tradeStart)
            else:
                raise Exception('Unsupported modelType = %s' % modelType)
            
            self._runGroups[sec] = InstrumentedStrategy(strategy)
            
        self._trading = False
        self._posfile = open(positionsFile,"w")
        self._equityfile = open(equityFile,"w")
        self._returnsfile = open(returnsFile,"w")
        if summaryFile:
            self._summaryfile = open(summaryFile,"w")
        else:
            self._summaryfile = open("/dev/null","w")
        
        self._dd = DrawDown()
        self._dd.attached(self)
        
    def getBroker(self):
        return self._broker

    def getEquity(self):
        return self._broker.getCash()
    
    def drawdown(self):
        return self._dd
    
    def net_return(self):
        return self._totalProfit / self._startingCash
    
    def get_net_profit(self):
        return self._totalProfit
    
    def get_trade_profit(self):
        return self._tradeProfit
    
    def run(self, feedStart, tradeStart, tradeEnd):
        # sanity check our parms
        assert(feedStart <= tradeStart)
        assert(tradeStart <= tradeEnd)
        
        # set each feed cursor to our feedStart
        for sec in self._runGroups:
            self._runGroups[sec].feed().set_cursor(feedStart)
            
        # emit bars one datetime at a time
        nextDateTime = self._feed.get_next_bars_date() 
        while nextDateTime != None and nextDateTime < tradeEnd:
            if not self._trading:
                if nextDateTime >= tradeStart:
                    self._trading = True
                    self._handle_trade_start(nextDateTime)
                    
            #print 'emitting bars for date %s' % nextDateTime
            lastEmitDate = nextDateTime
            self._feed.start(last=nextDateTime)
            
            nextDateTime = self._feed.get_next_bars_date()
            if self._trading and nextDateTime != None:
                self._print_sector_equity(lastEmitDate)
                self._dd.beforeOnBars(self)
            
        self._handle_trade_end(lastEmitDate)
        
    def writeAllTrades(self, filename):
        # get one list with all trades
        alltrades = []
        for sec in self._runGroups:
            alltrades.extend(self._runGroups[sec].trades_analyzer().trade_records())
        
        # now we want to sort this by trade entry
        alltrades = sorted(alltrades, key=lambda x: x.getEntryDate())
        
        # want to sum total trade profit for verification purposes
        self._tradeProfit = 0.0
                
        file_ = open(filename,'w')
        # write the header row
        file_.write('description,symbol,units,entryDate,entryPrice,exitDate,exitPrice,commissions,profitLoss\n')
        for t in alltrades:
            self._tradeProfit += t.getNetProfit(0)
            file_.write('%s,%s,%d,%s,%f,%s,%f,%0.2f,%0.2f\n' % 
                        (self._db.get(t.getSymbol()).description(),
                         t.getSymbol(),
                         t.getTradeSize(),
                         t.getEntryDate(),
                         t.getEntryPrice(),
                         t.getExitDate(),
                         t.getExitPrice(),
                         t.getCommissions(),
                         t.getNetProfit(0)))
        file_.close()
        
    def _handle_trade_start(self, datetime):
        self._broker.setCash(self._startingCash)
        for sec in self._runGroups:
            self._reset_broker(self._runGroups[sec])
        self._print_sector_positions(datetime)
        
    def _handle_trade_end(self, datetime):
        # want to report our positions before exiting them
        self._print_sector_positions(datetime)

        # exit all positions - this needs to happen before we report final equity and returns
        for sec in self._runGroups:
            self._runGroups[sec].strategy().exitPositions()

        self._dd.beforeOnBars(self)
        self._print_sector_equity(datetime)
        self._print_returns_summary(datetime, final=True)
        self._print_sector_returns()
        self._posfile.close()
        self._equityfile.close()
        self._returnsfile.close()
        self._summaryfile.close()
     
    def _reset_broker(self, istrat):
        '''Resets the equity in the broker to our starting cash position.'''
        # this is kind of a kludge but it's nice to be able to sum the trade profits
        # and match the equity amounts (from startingCash).  We need to adjust the 
        # starting cash down by the commission required to enter into our initial positions
        # since they are counted against us in the trade
        entry_commission = istrat.trades_analyzer().reset(istrat.strategy().getBroker().get_last_mark_to_market())
        self._broker.setCash(self._broker.getCash() - entry_commission)
        
    def _print_sector_returns(self):
        try:
            self._returnheader
        except:
            # means we need to print headers
            self._returnheader = True
            str_ = ''
            for sec in sorted(self._runGroups):
                str_ = str_ + '%s-Long %%,%s-Short %%,%s-Total %%,' % (sec,sec,sec)
            str_ = str_ + 'Total Long %,Total Short %,Total %'
            self._returnsfile.write('%s\n' % str_)

        total_long_profit = 0.0
        total_profit = 0.0
        str_ = ''
        for sec in sorted(self._runGroups):
            profit = 0.0
            long_profit = 0.0
            for trade in self._runGroups[sec].trades_analyzer().trade_records():
                profit += trade.getNetProfit(0)
                if trade.getTradeSize() > 0:
                    long_profit += trade.getNetProfit(0)
            short_profit = profit - long_profit
            str_ = str_ + '%0.1f,%0.1f,%0.1f,' % \
                    (long_profit / self._startingCash * 100.0, 
                     short_profit / self._startingCash * 100.0,
                     profit / self._startingCash * 100.0)
            total_long_profit += long_profit
            total_profit += profit
        total_short_profit = total_profit - total_long_profit
        str_ = str_ + '%0.1f,%0.1f,%0.1f' % \
                (total_long_profit / self._startingCash * 100.0, 
                 total_short_profit / self._startingCash * 100.0,
                 total_profit / self._startingCash * 100.0)
        self._returnsfile.write('%s\n' % str_)
        self._totalProfit = total_profit

    def _print_returns_summary(self, datetime, final=False): 
        try:
            self._summaryHeader
        except:
            # means we need to print headers
            self._summaryHeader = True
            self._sumCurMonth = None
            self._sumCurYear = None
            str_ = 'Year,Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec,Full Year'
            self._summaryfile.write('%s\n' % str_)

        if self._sumCurYear != datetime.year or final:
            if self._sumCurYear or final:
                # this means the year just rolled on us and we have to output a new row
                self._sumMonthData[self._sumCurMonth-1] = (self.getEquity() - self._sumMonthStart) / self._sumMonthStart * 100.0
                str_ = '%s,' % self._sumCurYear
                for r in self._sumMonthData:
                    str_ = str_ + '%0.1f,' % r
                str_ = str_ + '%0.1f' % (((self.getEquity() - self._sumYearStart) / self._sumYearStart) * 100.0)
                self._summaryfile.write('%s\n' % str_)                
            
            # now reset and get ready for this year
            self._sumCurYear = datetime.year
            self._sumCurMonth = datetime.month
            self._sumMonthData = numpy.zeros(12)
            self._sumYearStart = self.getEquity()
            self._sumMonthStart = self.getEquity()
        elif self._sumCurMonth != datetime.month:
            # the month rolled on us so put a new entry in
            self._sumMonthData[self._sumCurMonth-1] = (((self.getEquity() - self._sumMonthStart) / self._sumMonthStart)*100.0) 
            self._sumMonthStart = self.getEquity()
            self._sumCurMonth = datetime.month
        else:
            # nothing to do - just a mid-month data point
            pass
            
    def _print_sector_equity(self, datetime):
        self._print_returns_summary(datetime) 
        try:
            self._equityheader
        except:
            # means we need to print headers
            self._equityheader = True
            str_ = 'Datetime,'
            for sec in sorted(self._runGroups):
                str_ = str_ + '%s-Equity,%s-Margin,' % (sec,sec)
            str_ = str_ + 'Total-Equity,Total-Margin'
            self._equityfile.write('%s\n' % str_)

        # going to build a row of text for output to the equity report
        str_ = '%s,' % datetime
        total_equity = 0.0
        total_margin = 0.0
        for sec in sorted(self._runGroups):
            equity = self._runGroups[sec].getEquity()
            margin = self._runGroups[sec].calc_margin()
            str_ = str_ + '%0.2f,%0.2f,' % (equity,margin)
            total_equity += equity
            total_margin += margin
        str_ = str_ + '%0.2f,%0.2f' % (self.getEquity()-self._startingCash,total_margin)
        self._equityfile.write('%s\n' % str_)
          
    def _print_sector_positions(self, datetime):
        try:
            self._posheader
        except:
            # means we need to print headers
            self._posheader = True
            str_ = 'Datetime,'
            for sec in sorted(self._runGroups):
                str_ = str_ + '%s-Long,%s-Short,' % (sec,sec)
            str_ = str_ + 'Total-Long,Total-Short'
            self._posfile.write('%s\n' % str_)

        # going to build a row of text for output to the positions report
        str_ = '%s,' % datetime
        total_longs = 0
        total_shorts = 0
        for sec in sorted(self._runGroups):
            longs = 0
            shorts = 0
            sec_positions = self._runGroups[sec].strategy().getPositions() 
            for sym in sec_positions:
                if sec_positions[sym].isLong():
                    longs = longs + 1
                else:
                    shorts = shorts + 1
            str_ = str_ + '%s,%s,' % (longs, shorts)
            total_longs += longs
            total_shorts += shorts
        # now the totals
        str_ = str_ + '%s,%s' % (total_longs, total_shorts)
        self._posfile.write('%s\n' % str_)