# All_Tieba_Sign_Mobile
一个用于获取所有关注的贴吧、并模拟手机端进行签到的脚本。

网上大部分的脚本由于接口原因只能获取前200个，这个脚本可以获取所有关注的贴吧，只需在cookie里找到并填入BDUSS和STOKEN。

目前唯一的问题是STOKEN过期时间，~~据说只有一个月~~（好像用了快半年也没变）。如果你关注的贴吧没有变化的话，可以注释掉`get_all_forums_fid(bduss, stoken)`函数，签到函数只需要BDUSS。

![image1](/pic/image1.png)

![image2](/pic/image2.png)
