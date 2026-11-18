// Offline examples run entirely on the device and incur no usage charges.
function tideExample(messages) {
  const last=messages.at(-1).content;
  const prior=messages.filter(x=>x.role==='user').at(-2)?.content || '';
  if(last.includes('久没联系') || last.includes('朋友写')) return '可以从一件让你想到对方的小事说起。比如：\n\n“今天路过以前常去的那家店，突然想起咱们坐在窗边聊天的下午。好久没联系了，你最近过得怎么样？”\n\n把那家店换成你们共同熟悉的地方，这段问候就会更像你亲口说的话。';
  if(last.includes('先做哪') || last.includes('梳理思路')) return '把手头的事列出来就好，一行一件。然后给每件事补两个信息：最晚什么时候做，以及今天能迈出的第一小步。\n\n比如“周五交方案 → 今天先写三个小标题”。你最惦记的是哪一件？';
  if(last.includes('过得怎么样') || last.includes('找个人聊聊')) return '今天过得怎么样？可以从一个很小的瞬间说起。是什么让你停下来，想找个人聊一聊？';
  if(last.includes('【附件')) return '已读入这份文本。开头的内容是：\n\n'+last.split('】').at(-1).trim().slice(0,260)+'\n\n这是本地摘录。连接模型后可以继续总结、改写或提问。';
  if(prior.includes('聊聊')) return '我看到你写下了“'+last.slice(0,64)+'”。如果把今天讲成一个小故事，你会从哪个时刻开始？';
  if(prior.includes('先做哪')) return '可以从你刚才列出的事情里选一件，写下一个十分钟内能完成的动作。做完后，再看要不要继续。';
  return '你刚才写的是“'+last.slice(0,80)+'”。当前为离线演示，你可以试试首页的「随心聊聊」「梳理思路」或「写点东西」。登录已配置模型的服务后，这里会显示针对你问题生成的回答。';
}
